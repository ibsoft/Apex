"""YouTube search and player control for the APEX music and video windows.

The backend never downloads or plays anything: it searches with the ``yt-dlp``
binary and tells the browser what to play through ``ctx.emit``. Playback, queue
and transport state all live in the frontend window that owns the iframe, so
this module stays a thin, side-effect-free adapter around one subprocess.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from tools.base import Tool

#: The two windows a media request can address. "music" hides the picture
#: behind artwork; "video" shows the iframe with its own transport.
APPS = ("music", "video")

#: Every action the tool accepts. ``status`` never touches the player; the
#: browser reports what is playing in the non-persisted ``media_context``
#: block, so a "what is playing" question is answered from context.
ACTIONS = (
    "play", "search", "stop", "pause", "resume", "next", "previous",
    "close", "status", "maximize", "minimize", "restore",
)

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,24}$")

#: A short-lived cache of successful searches, keyed by the normalized query and
#: the limit. The search subprocess is the only call this app makes to YouTube,
#: and asking twice in a row ("play music by X", then "play it again") does not
#: need a second request: a connection that looks like it is hammering YouTube
#: gets flagged, and a flagged connection has *embedded playback refused in the
#: browser*, which no amount of retrying on our side can undo.
_SEARCH_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_MAX = 64


def _cache_ttl(config) -> float:
    try:
        return max(0.0, float(getattr(config, "YOUTUBE_SEARCH_CACHE_SECONDS", 300) or 0))
    except (TypeError, ValueError):
        return 300.0


def clear_search_cache() -> None:
    """Test seam: module state, so a suite must not inherit another test's hits."""
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE.clear()


def _cache_key(query: str, limit: int) -> tuple[str, int]:
    return (" ".join(query.lower().split()), limit)


def _cache_get(key: tuple[str, int], ttl: float) -> list[dict] | None:
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _SEARCH_CACHE_LOCK:
        hit = _SEARCH_CACHE.get(key)
        if hit is None:
            return None
        stored_at, items = hit
        if now - stored_at > ttl:
            _SEARCH_CACHE.pop(key, None)
            return None
        # Copies: the caller may hand these to the model and the browser, and a
        # mutation of a cached entry would leak into the next hit.
        return [dict(item) for item in items]


def _cache_put(key: tuple[str, int], items: list[dict]) -> None:
    with _SEARCH_CACHE_LOCK:
        if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[key] = (time.monotonic(), [dict(item) for item in items])


def _settings(config) -> tuple[bool, str, int, float]:
    enabled = bool(getattr(config, "YOUTUBE_ENABLED", True))
    binary = str(getattr(config, "YOUTUBE_DLP", "") or "").strip() or "yt-dlp"
    limit = int(getattr(config, "YOUTUBE_SEARCH_LIMIT", 8) or 8)
    timeout = float(getattr(config, "YOUTUBE_SEARCH_TIMEOUT", 20) or 20)
    return enabled, binary, max(1, min(limit, 25)), max(5.0, min(timeout, 120.0))


def _resolve_binary(binary: str) -> str | None:
    """Absolute path of the binary, or None when it cannot be executed.

    ``Path("")`` is ``.``, which exists and is a directory - an unset
    YOUTUBE_DLP falls back to the bare name before this runs, and a name with
    a separator is checked as a real executable file rather than through
    ``which``, which does not accept one."""
    if os.sep in binary or (os.altsep and os.altsep in binary):
        try:
            return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
        except OSError:
            return None
    return shutil.which(binary)


def _parse_entry(line: str) -> dict | None:
    """One yt-dlp JSON line into a playback item, or None to skip it.

    Every field is read defensively: a flat playlist entry has no thumbnail and
    may have no channel, and one malformed line must not throw away the rest of
    the results."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        raw: Any = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    video_id = str(raw.get("id") or "").strip()
    if not _ITEM_ID_RE.fullmatch(video_id):
        return None
    title = str(raw.get("title") or "").strip() or "Untitled"
    channel = str(raw.get("channel") or raw.get("uploader") or "").strip()
    duration = raw.get("duration")
    try:
        seconds = max(0, int(float(duration))) if duration is not None else 0
    except (TypeError, ValueError):
        seconds = 0
    return {
        "id": video_id,
        "title": title[:300],
        "channel": channel[:200],
        "duration": seconds,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def search_youtube(query: str, config) -> list[dict]:
    """Top-N YouTube results for ``query`` as playback items.

    Uses the yt-dlp binary in a flat-playlist search: fast, no video extraction,
    no download. Raises only what the caller turns into a plain message - the
    handler never lets a subprocess failure escape."""
    enabled, binary, limit, timeout = _settings(config)
    if not enabled:
        raise RuntimeError("YouTube playback is disabled in APEX settings.")
    if _resolve_binary(binary) is None:
        raise RuntimeError(f"yt-dlp is not installed (looked for `{binary}`).")
    key = _cache_key(query, limit)
    ttl = _cache_ttl(config)
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached
    command = [
        binary,
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "--skip-download",
        "--playlist-end", str(limit),
        f"ytsearch{limit}:{query}",
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    items: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        item = _parse_entry(line)
        if item is None:
            continue
        if any(existing["id"] == item["id"] for existing in items):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    if not items:
        tail = (proc.stderr or "").strip().splitlines()[-1:]
        detail = f" ({tail[0].strip()[:200]})" if tail else ""
        raise RuntimeError(f"no results{detail}")
    _cache_put(key, items)
    return items


def _format_results(items: list[dict], query: str) -> str:
    lines = [f"YouTube results for {query!r}:"]
    for i, item in enumerate(items, start=1):
        channel = f" — {item['channel']}" if item["channel"] else ""
        lines.append(f"{i}. {item['title']}{channel} [{item['id']}]")
    lines.append('Call again with action="play" and pick=<number> to start one.')
    return "\n".join(lines)


def t_media(args: dict, ctx, config) -> str:
    enabled, binary, _limit, timeout = _settings(config)
    app = str(args.get("app") or "").strip().lower()
    if app not in APPS:
        return 'Say which player to use: app="music" (audio) or app="video" (video window).'
    action = str(args.get("action") or "").strip().lower() or "play"
    if action not in ACTIONS:
        return f"Unknown action `{action}`. Use one of: {', '.join(ACTIONS)}."
    if not enabled:
        return "YouTube playback is turned off in APEX settings (YOUTUBE_ENABLED)."

    emit = ctx.emit if getattr(ctx, "emit", None) else (lambda _event: None)

    if action in ("play", "search"):
        query = str(args.get("query") or "").strip()
        if not query:
            return 'Say what to play: action="play" needs a query, for example "lofi jazz".'
        try:
            items = search_youtube(query, config)
        except subprocess.TimeoutExpired:
            return f"YouTube search timed out after {timeout:.0f}s; try a shorter query."
        except RuntimeError as exc:
            return f"YouTube search failed: {exc}"
        except Exception as exc:  # a broken binary must be a message, never a raise
            return f"YouTube search failed: {type(exc).__name__}: {exc}"
        try:
            pick = int(args.get("pick") or 1)
        except (TypeError, ValueError):
            pick = 1
        pick = min(max(pick, 1), len(items))
        if action == "search":
            return _format_results(items, query)
        chosen = items[pick - 1]
        emit({"type": "media_play", "app": app, "items": items, "index": pick - 1})
        where = "Music" if app == "music" else "Video"
        return f"Playing {chosen['title']!r} in the {where} window" + (
            f" (result {pick} of {len(items)} for {query!r})." if len(items) > 1 else "."
        )

    if action == "status":
        # The live player state reaches the model as media_context on every
        # turn, so status is a report-the-context action, not a query.
        return ("Status comes from the media context block of this turn; "
                "if it names nothing, no player is open.")
    if action in ("pause", "resume", "stop", "close", "next", "previous",
                  "maximize", "minimize", "restore"):
        emit({"type": "media_control", "app": app, "action": action})
        where = "Music" if app == "music" else "Video"
        return f"{action.capitalize()} request sent to the {where} player."
    return f"Unknown action `{action}`. Use one of: {', '.join(ACTIONS)}."


def build_media_tools(config) -> list[Tool]:
    """The model-facing media tool. Registered unconditionally, like VISIO:
    the gate is inside the handler, so flipping YOUTUBE_ENABLED in settings
    applies on the next turn instead of needing a restart."""
    return [
        Tool(
            name="media_player",
            description=(
                "Search and control the on-screen YouTube players. "
                'action="play" searches and starts the chosen result in a '
                'window (app="music" for audio with artwork, app="video" for '
                'the video window); action="search" lists results without '
                'playing; the transport actions (stop, pause, resume, next, '
                "previous, close, maximize, minimize, restore) act on the "
                "player of that app. play/search need a query; pick selects a "
                "result from a previous search (1-based)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "enum": list(APPS),
                        "description": 'Which player: "music" or "video".',
                    },
                    "action": {
                        "type": "string",
                        "enum": list(ACTIONS),
                        "description": "What to do.",
                    },
                    "query": {
                        "type": "string",
                        "description": "What to search for on YouTube (play/search).",
                    },
                    "pick": {
                        "type": "integer",
                        "description": "1-based result to start from a search (default 1).",
                    },
                },
                "required": ["app", "action"],
                "additionalProperties": False,
            },
            handler=lambda args, ctx: t_media(args, ctx, config),
        ),
    ]
