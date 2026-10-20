"""Local music library: download YouTube audio as MP3 and play it from disk.

The YouTube embed refuses some videos outright with error 150 ("embedding
disabled by the owner"), and nothing this app does can change that. This module
is the way around it: the same ``yt-dlp`` binary that already searches is asked
to extract the audio of one video as an MP3, the file lands in a per-user music
directory (plus the operator's own ``~/Music``), and the browser streams it back
through a signed URL. Progress is pushed to the browser through ``ctx.emit`` so
the window can show a download bar instead of a frozen "playing".

Two surfaces:

- ``t_music_library`` - the model-facing tool: ``download`` (search + fetch),
  ``list`` (the local library) and ``play`` (queue local tracks).
- ``register_music_routes`` - ``GET /api/music/stream/<token>`` (Range-aware
  audio) and ``GET /api/music/library`` (the signed library listing the UI uses
  to show what is available offline).

Nothing here trusts the browser or the model with a path: the library is
resolved inside the configured roots, and streaming goes through a signed,
owner-bound token like every other file route.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolContext

#: A YouTube id has this shape; a track in the library is named after it so the
#: same video downloaded twice is recognised rather than duplicated.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,24}$")

#: How much of a download the progress line may be trusted to report; the events
#: are throttled so a multi-minute download does not flood the SSE stream.
_PROGRESS_MIN_INTERVAL = 0.4

#: Signing salt for the streaming ticket. The owner is carried inside it, so a
#: token for one account cannot address another's file.
_STREAM_SALT = "apex-music-stream-v1"
_LIBRARY_SALT = "apex-music-library-v1"
#: Long enough for an album listening session; short enough that a leaked token
#: is not forever. The browser re-requests the library if it expires.
_STREAM_TTL_SECONDS = 12 * 3600


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _settings(config) -> dict:
    def _roots() -> list[Path]:
        raw = getattr(config, "MUSIC_LIBRARY_ROOTS", []) or []
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,\n]", raw)]
        roots: list[Path] = []
        for entry in raw:
            text = str(entry).strip()
            if not text:
                continue
            try:
                roots.append(Path(text).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
        return roots

    def _exts() -> tuple[str, ...]:
        raw = getattr(config, "MUSIC_AUDIO_EXTENSIONS", None) or [".mp3"]
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,\n]", raw)]
        exts = []
        for entry in raw:
            text = str(entry).strip().lower()
            if not text:
                continue
            exts.append(text if text.startswith(".") else f".{text}")
        return tuple(exts) or (".mp3",)

    return {
        "enabled": bool(getattr(config, "MUSIC_ENABLED", True)),
        "dir": Path(getattr(config, "MUSIC_DIR", Path("."))).expanduser(),
        "roots": _roots(),
        "extensions": _exts(),
        "binary": str(getattr(config, "YOUTUBE_DLP", "") or "").strip() or "yt-dlp",
        "audio_format": str(getattr(config, "MUSIC_AUDIO_FORMAT", "mp3") or "mp3").strip().lower() or "mp3",
        "audio_quality": str(getattr(config, "MUSIC_AUDIO_QUALITY", "192") or "192").strip() or "192",
        "timeout": max(30.0, float(getattr(config, "MUSIC_DOWNLOAD_TIMEOUT", 600) or 600)),
        "library_max": max(1, int(getattr(config, "MUSIC_LIBRARY_MAX", 500) or 500)),
        "fallback": bool(getattr(config, "MUSIC_FALLBACK_TO_YOUTUBE", True)),
    }


def _resolve_binary(binary: str) -> str | None:
    if os.sep in binary or (os.altsep and os.altsep in binary):
        try:
            return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
        except OSError:
            return None
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

@dataclass
class Track:
    """One local audio file, addressed by an opaque id the token signs."""

    id: str            # stable key: "<scope>:<relpath>" (never a raw path)
    title: str
    path: Path
    size_bytes: int
    mtime: float
    duration: int = 0
    source: str = "local"   # "download" | "local"
    video_id: str = ""

    def as_item(self, config, user_id: str) -> dict:
        url = stream_url(config, user_id, self.id)
        return {
            "id": self.id,
            "title": self.title,
            "channel": "",
            "duration": self.duration,
            "url": url,
            "source": "local",
            "thumbnail": "",
        }


#: Library scans hold no state beyond this process-wide lock, used only to keep
#: two concurrent scans from racing on the same directory listing.
_SCAN_LOCK = threading.Lock()


def _library_root(config) -> Path:
    return Path(_settings(config)["dir"])


def _user_download_dir(config, user_id: str) -> Path:
    """The per-user download directory under MUSIC_DIR.

    The user id comes from the session, but it still has to be a plain directory
    name - a slash or ".." in it would address files outside the music root."""
    name = str(user_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
        name = "shared"
    directory = (_library_root(config) / name).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _user_owns_dir(config, user_id: str) -> Path | None:
    """The user's own download directory, without creating it (for scanning)."""
    name = str(user_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
        return None
    try:
        return (_library_root(config) / name).resolve()
    except OSError:
        return None


def _scan_roots(config, user_id: str) -> list[tuple[str, Path]]:
    """(scope, directory) pairs to scan, scoped so a track id is unambiguous.

    ``own`` is the signed-in user's download directory; ``local`` covers the
    conventional music roots (``~/Music`` …). A caller never supplies a path.
    """
    pairs: list[tuple[str, Path]] = []
    own = _user_owns_dir(config, user_id)
    if own is not None:
        pairs.append(("own", own))
    for root in _settings(config)["roots"]:
        if root.is_dir():
            pairs.append(("local", root))
    return pairs


def _title_from_name(name: str) -> str:
    stem = Path(name).stem
    # "Artist - Title [abcdefghijklm]" -> "Artist - Title"; the id tag yt-dlp
    # appends is metadata, not something to read out.
    stem = re.sub(r"\s*\[[A-Za-z0-9_-]{6,24}\]\s*$", "", stem).strip()
    return stem or Path(name).name


def _read_duration(path: Path) -> int:
    """Best-effort seconds from ffprobe; 0 when unavailable (not an error)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return max(0, int(float((result.stdout or "0").strip() or 0)))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def _track_id(scope: str, path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    return f"{scope}:{rel.as_posix()}"


def _decode_track_id(track_id: str, user_id: str, config) -> Path | None:
    """Resolve a signed track id back to a real file inside an allowed root.

    The id is ``<scope>:<relpath>``; the scope decides which root it is relative
    to, and the resolved path is checked to stay inside that root - a crafted id
    can never walk out of the library."""
    if not isinstance(track_id, str) or ":" not in track_id:
        return None
    scope, _, rel = track_id.partition(":")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    root: Path | None = None
    if scope == "own":
        root = _user_owns_dir(config, user_id)
    elif scope == "local":
        # A local track must match exactly one of the configured roots; the id
        # stores the path relative to that root, so we test each.
        for candidate in _settings(config)["roots"]:
            target = (candidate / rel).resolve()
            if target.is_relative_to(candidate.resolve()) and target.is_file():
                return target
        return None
    if root is None:
        return None
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def list_library(config, user_id: str, query: str = "", limit: int = 100) -> list[Track]:
    """Every readable audio track in the scoped roots, newest first.

    A query filters on the filename (case-insensitive substring); an empty query
    lists the whole library. Symlinks are skipped, as in every other scanner."""
    exts = _settings(config)["extensions"]
    needle = (query or "").strip().casefold()
    found: list[Track] = []
    with _SCAN_LOCK:
        for scope, root in _scan_roots(config, user_id):
            for base, dirnames, filenames in os.walk(root, followlinks=False):
                # Do not descend into hidden or virtual trees.
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not name.lower().endswith(exts):
                        continue
                    path = Path(base) / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        info = path.stat()
                    except OSError:
                        continue
                    if needle and needle not in name.casefold():
                        continue
                    video_id = ""
                    match = re.search(r"\[([A-Za-z0-9_-]{6,24})\]", name)
                    if match:
                        video_id = match.group(1)
                    found.append(Track(
                        id=_track_id(scope, path, root),
                        title=_title_from_name(name),
                        path=path,
                        size_bytes=info.st_size,
                        mtime=info.st_mtime,
                        source="download" if scope == "own" else "local",
                        video_id=video_id,
                    ))
    found.sort(key=lambda t: t.mtime, reverse=True)
    capped = found[: max(1, min(int(limit), 1000))]
    for track in capped:
        track.duration = _read_duration(track.path)
    return capped


def library_has_video(config, user_id: str, video_id: str) -> Track | None:
    """The downloaded track for a video id, if it is already in the library."""
    if not _VIDEO_ID_RE.fullmatch(str(video_id or "")):
        return None
    for track in list_library(config, user_id, query="", limit=1000):
        if track.video_id == video_id:
            return track
    return None


# ---------------------------------------------------------------------------
# streaming + library tokens
# ---------------------------------------------------------------------------

def _signer(config, salt: str):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(getattr(config, "SECRET_KEY", "dev-change-me"), salt=salt)


def stream_url(config, user_id: str, track_id: str) -> str:
    from public_urls import public_url

    token = _signer(config, _STREAM_SALT).dumps({"u": str(user_id), "t": str(track_id)})
    return public_url(config, f"/api/music/stream/{token}")


def library_url(config, user_id: str) -> str:
    from public_urls import public_url

    token = _signer(config, _LIBRARY_SALT).dumps({"u": str(user_id)})
    return public_url(config, f"/api/music/library/{token}")


def _stream_ticket(config, token: str) -> dict | None:
    from itsdangerous import BadSignature, SignatureExpired

    try:
        ticket = _signer(config, _STREAM_SALT).loads(token, max_age=_STREAM_TTL_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    return ticket if isinstance(ticket, dict) else None


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def _parse_progress(line: str) -> dict | None:
    """Read one yt-dlp progress line into a percentage, or None to ignore it.

    yt-dlp with ``--newline`` prints ``[download]  12.3% of 4.56MiB at ...``.
    Parsing the percentage (rather than a byte count) is enough for a progress
    bar and survives the many different byte-unit spellings."""
    match = re.search(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%", line or "")
    if not match:
        return None
    try:
        percent = float(match.group(1))
    except ValueError:
        return None
    return {"percent": max(0.0, min(100.0, percent))}


def _download_one(config, user_id: str, item: dict, emit) -> Track:
    """Download one search result to MP3 and return the stored track.

    Raises ``RuntimeError`` with a plain sentence on any failure; the caller
    turns that into the tool result. Progress is pushed through ``emit`` with a
    throttle so a long download cannot flood the SSE stream."""
    settings = _settings(config)
    binary = _resolve_binary(settings["binary"])
    if binary is None:
        raise RuntimeError(f"yt-dlp is not installed (looked for `{settings['binary']}`).")
    if not shutil.which("ffmpeg") and settings["audio_format"] != "wav":
        raise RuntimeError("ffmpeg is not installed; it is required to extract MP3 audio.")

    video_id = str(item.get("id") or "")
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise RuntimeError("that result has no usable video id.")
    directory = _user_download_dir(config, user_id)
    outtmpl = str(directory / "%(title)s [%(id)s].%(ext)s")

    command = [
        binary,
        "--no-playlist",
        "--newline",
        "-x",                                   # extract audio
        "--audio-format", settings["audio_format"],
        "--audio-quality", settings["audio_quality"],
        "-o", outtmpl,
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    def _emit_progress(percent: float, stage: str = "downloading"):
        if not emit:
            return
        try:
            emit({"type": "media_progress", "stage": stage, "percent": round(percent, 1),
                  "id": video_id, "title": item.get("title", "")})
        except Exception:
            pass

    _emit_progress(0.0, "starting")
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    last = 0.0
    deadline = time.monotonic() + settings["timeout"]
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError("the download timed out.")
            parsed = _parse_progress(line)
            if parsed is None:
                # Conversion stage: ffmpeg post-processing, reported once.
                if "[ExtractAudio]" in line or "[ffmpeg]" in line:
                    _emit_progress(99.0, "converting")
                continue
            percent = parsed["percent"]
            now = time.monotonic()
            if percent >= 100 or now - last >= _PROGRESS_MIN_INTERVAL:
                last = now
                _emit_progress(percent)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("the download timed out.") from None
    finally:
        try:
            proc.stdout and proc.stdout.close()
        except OSError:
            pass

    if proc.returncode != 0:
        raise RuntimeError("yt-dlp could not download this track; it may be region-locked or removed.")

    # Find the file just written: yt-dlp names it "<title> [<id>].<ext>".
    target = _find_downloaded(directory, video_id, settings["extensions"])
    if target is None:
        raise RuntimeError("the download finished but the file could not be found.")
    _prune_library(config, user_id, settings["library_max"])
    _emit_progress(100.0, "done")
    track = Track(
        id=_track_id("own", target, directory),
        title=_title_from_name(target.name),
        path=target,
        size_bytes=target.stat().st_size,
        mtime=target.stat().st_mtime,
        source="download",
        video_id=video_id,
    )
    track.duration = _read_duration(target)
    return track


def _find_downloaded(directory: Path, video_id: str, extensions: tuple[str, ...]) -> Path | None:
    try:
        candidates = [
            path for path in directory.iterdir()
            if path.is_file() and path.name.endswith(f"[{video_id}]" + path.suffix)
            and path.suffix.lower() in extensions
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _prune_library(config, user_id: str, maximum: int) -> None:
    """Keep the user's download directory to ``maximum`` tracks, oldest first."""
    directory = _user_owns_dir(config, user_id)
    if directory is None:
        return
    exts = _settings(config)["extensions"]
    try:
        files = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in exts),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[maximum:]:
        try:
            stale.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------

def _format_library(tracks: list[Track], query: str) -> str:
    if not tracks:
        return (f"No local tracks found for {query!r}." if query
                else "The local music library is empty.")
    lines = [f"Local tracks ({len(tracks)}):"]
    for i, track in enumerate(tracks[:25], start=1):
        mins = f" [{track.duration // 60}:{track.duration % 60:02d}]" if track.duration else ""
        lines.append(f"{i}. {track.title}{mins}")
    if len(tracks) > 25:
        lines.append(f"… and {len(tracks) - 25} more.")
    return "\n".join(lines)


def t_music_library(args: dict, ctx, config) -> str:
    settings = _settings(config)
    if not settings["enabled"]:
        return "The local music library is turned off in APEX settings (MUSIC_ENABLED)."
    if not ctx.user_id:
        return "Sign in to use the local music library."
    action = str(args.get("action") or "list").strip().lower()
    emit = ctx.emit if getattr(ctx, "emit", None) else None

    if action == "list":
        query = str(args.get("query") or "").strip()
        tracks = list_library(config, ctx.user_id, query=query, limit=100)
        if emit:
            emit({"type": "media_library", "count": len(tracks)})
        return _format_library(tracks, query)

    if action == "play":
        query = str(args.get("query") or "").strip()
        tracks = list_library(config, ctx.user_id, query=query, limit=50)
        if not tracks:
            return (f"No local track matches {query!r}. Use action=\"download\" first, "
                    "or search YouTube with media_player.")
        items = [track.as_item(config, ctx.user_id) for track in tracks]
        try:
            pick = int(args.get("pick") or 1)
        except (TypeError, ValueError):
            pick = 1
        pick = min(max(pick, 1), len(items))
        if emit:
            emit({"type": "media_play", "app": "music", "items": items,
                  "index": pick - 1, "source": "local"})
        return (f"Playing {items[pick - 1]['title']!r} from the local library"
                + (f" (result {pick} of {len(items)})." if len(items) > 1 else "."))

    if action == "download":
        query = str(args.get("query") or "").strip()
        video_arg = str(args.get("video_id") or "").strip()
        # A named video id is a complete request on its own (the window's
        # "embedding disabled" fallback sends one); only a search needs a query.
        if not query and not video_arg:
            return 'Say what to download: action="download" needs a query or a video_id.'
        # Imported lazily: the search helper lives in youtube_tools and pulling
        # it in at module load would make the two modules' registration order
        # matter.
        from tools.youtube_tools import search_youtube

        if video_arg:
            if not _VIDEO_ID_RE.fullmatch(video_arg):
                return "That video id is not valid."
            item = {"id": video_arg, "title": str(args.get("title") or query or video_arg)}
        else:
            try:
                results = search_youtube(query, config)
            except subprocess.TimeoutExpired:
                return "YouTube search timed out; try a shorter query."
            except RuntimeError as exc:
                return f"YouTube search failed: {exc}"
            if not results:
                return f"No YouTube results for {query!r}."
            try:
                pick = int(args.get("pick") or 1)
            except (TypeError, ValueError):
                pick = 1
            pick = min(max(pick, 1), len(results))
            item = results[pick - 1]

        # A track already in the library is played rather than re-downloaded.
        existing = library_has_video(config, ctx.user_id, str(item["id"]))
        if existing is not None:
            track = existing
            already = True
        else:
            try:
                track = _download_one(config, ctx.user_id, item, emit)
            except RuntimeError as exc:
                return f"Download failed: {exc}"
            except Exception as exc:  # a broken binary must be a message, never a raise
                return f"Download failed: {type(exc).__name__}: {exc}"
            already = False

        item_out = track.as_item(config, ctx.user_id)
        if emit:
            # Start playback of the freshly downloaded track in the music window.
            emit({"type": "media_play", "app": "music", "items": [item_out],
                  "index": 0, "source": "local"})
        verb = "Already in the library" if already else "Downloaded"
        return (f"{verb}: {track.title!r} ({track.size_bytes // 1024} KB). "
                "Playing it in the Music window.")

    return f"Unknown action `{action}`. Use list, play or download."


def build_music_tools(config) -> list[Tool]:
    return [
        Tool(
            name="music_library",
            description=(
                "The local music library: downloaded MP3s and local audio files. "
                'action="list" shows what is available offline (optional query); '
                'action="play" queues local tracks matching a query; '
                'action="download" searches YouTube and saves the chosen result as '
                "an MP3 into the library, showing progress, then plays it. Use "
                "download when a video refuses embedded playback (YouTube error "
                "150) or when the operator wants the song kept offline; use "
                "media_player for ordinary streaming playback."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "play", "download"]},
                    "query": {"type": "string", "description": "Title, artist or filename to find or download."},
                    "pick": {"type": "integer", "description": "1-based result to use (play/download)."},
                    "video_id": {"type": "string", "description": "Download this exact YouTube id instead of searching."},
                    "title": {"type": "string", "description": "Title to store when video_id is given."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=lambda args, ctx: t_music_library(args, ctx, config),
        ),
    ]


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def _range_bounds(header: str, size: int) -> tuple[int, int] | None:
    """Parse ``bytes=start-end`` into an inclusive range, or None if unusable.

    Audio elements issue Range requests to seek; answering 200 with the whole
    file makes seeking in a long track re-download from the start."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match:
        return None
    start_raw, end_raw = match.group(1), match.group(2)
    if start_raw == "" and end_raw == "":
        return None
    if start_raw == "":
        # Suffix range: the last N bytes.
        length = int(end_raw)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def register_music_routes(app, require_user, config):
    """Stream local audio and list the library, both behind owner-bound tokens."""
    from flask import Response, jsonify, request

    @app.get("/api/music/stream/<token>")
    def music_stream(token):
        user = require_user()
        if not user:
            return jsonify({"error": "Sign in to play this track."}), 401
        ticket = _stream_ticket(config, token)
        if ticket is None or str(ticket.get("u")) != str(user["id"]):
            # A forged, expired or foreign token is indistinguishable from a
            # track that never existed - the same rule as the other object routes.
            return jsonify({"error": "This track link is not valid."}), 404
        path = _decode_track_id(str(ticket.get("t") or ""), str(user["id"]), config)
        if path is None:
            return jsonify({"error": "This track is no longer available."}), 404
        try:
            size = path.stat().st_size
            handle = open(path, "rb")
        except OSError:
            return jsonify({"error": "This track is no longer available."}), 404

        content_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/*"
        range_header = request.headers.get("Range", "")
        bounds = _range_bounds(range_header, size)
        if bounds is None:
            response = Response(handle.read(), mimetype=content_type, direct_passthrough=True)
            response.headers["Content-Length"] = str(size)
            response.status_code = 200
        else:
            start, end = bounds
            handle.seek(start)
            chunk = handle.read(end - start + 1)
            response = Response(chunk, status=206, mimetype=content_type, direct_passthrough=True)
            response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            response.headers["Content-Length"] = str(len(chunk))
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, no-store"
        handle.close()
        return response

    @app.get("/api/music/library/<token>")
    def music_library(token):
        user = require_user()
        if not user:
            return jsonify({"error": "Sign in to see the music library."}), 401
        from itsdangerous import BadSignature, SignatureExpired

        try:
            ticket = _signer(config, _LIBRARY_SALT).loads(token, max_age=_STREAM_TTL_SECONDS)
        except (SignatureExpired, BadSignature):
            return jsonify({"error": "This library link is not valid."}), 404
        if not isinstance(ticket, dict) or str(ticket.get("u")) != str(user["id"]):
            return jsonify({"error": "This library link is not valid."}), 404
        return _library_payload(config, str(user["id"]), request)

    @app.get("/api/music")
    def music_library_session():
        """The signed-in user's library, no token needed: this is a normal
        authenticated API call the music window makes to browse local tracks."""
        user = require_user()
        if not user:
            return jsonify({"error": "Sign in to see the music library."}), 401
        return _library_payload(config, str(user["id"]), request)


def _library_payload(config, user_id: str, request):
    from flask import jsonify

    query = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    except (TypeError, ValueError):
        limit = 200
    tracks = list_library(config, user_id, query=query, limit=limit)
    return jsonify({
        "tracks": [
            {"id": t.id, "title": t.title, "duration": t.duration, "size_bytes": t.size_bytes,
             "source": t.source, "url": stream_url(config, user_id, t.id)}
            for t in tracks
        ],
        "count": len(tracks),
    })
