"""Telegram bot: talk to APEX from Telegram by text or voice notes.

The bot long-polls ``api.telegram.org`` with ``requests`` (already a backend
dependency; no python-telegram-bot). Each accepted message runs a normal agent
turn exactly like a scheduled task does - same provider, memory, skills and
tool registry. Text messages get the full answer back as plain text; a voice
note is transcribed and answered with a spoken ogg/opus voice note in the
configured reply language.

Security model: the bot answers **only** a chat id that has been paired with a
one-time pairing code. Pairing starts in the browser (the operator is the one
in front of the settings tab): the tab asks the backend for a code, shows it,
and the operator sends that code to the bot from their own Telegram account.
The chat id that redeems the code is the only chat the bot ever processes, and
turns run with the identity of the browser user who requested the code, so
memory, tools and terminal access are the operator's own. Messages from any
other chat are ignored, never answered.
"""
from __future__ import annotations

import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from tools.base import Tool

#: Settings keys the Telegram tab in the browser may write through the ordinary
#: settings POST. Every one is a non-secret flag or voice preference. The bot
#: token is deliberately not here: it is written only through its own validated
#: endpoint (``/api/telegram/token``) and stored in exactly one place, the
#: settings DB. It is never mirrored into an env file - a second plaintext copy
#: would be readable by anyone who can read the repo, and an env fallback would
#: resurrect a token the operator asked to forget.
SETTING_KEYS = (
    "telegram_enabled",
    "telegram_voice_replies",
    "telegram_tts_engine",
    "telegram_tts_voice",
)

#: Settings keys the bot manages itself and that are never writable from
#: ``/api/settings`` (the pairing flow owns them). ``public_settings`` drops
#: them so a chat id cannot round-trip through the browser.
MANAGED_KEYS = (
    "telegram_paired_chat_id",
    "telegram_paired_username",
    "telegram_paired_user_id",
    "telegram_paired_at",
)

ALLOWED_TTS_ENGINES = ("espeak", "edge")

_API = "https://api.telegram.org"
# Telegram caps a message body at 4096 chars and a voice-note caption at 1024.
_TEXT_MAX = 4096
_CAPTION_MAX = 1024
#: Hard cap on anything fetched through getFile: a voice note is a few MB, so
#: anything past this is hostile or broken, not a long recording.
_MEDIA_MAX_BYTES = 40 * 1024 * 1024
#: Telegram's per-file upload cap; anything a tool points at past this is
#: refused before the upload starts. Images additionally go out compressed via
#: sendPhoto only below the photo cap (10 MB, Telegram's own limit); bigger
#: images are sent as documents, which preserves the original bytes.
_SEND_MAX_BYTES = 50 * 1024 * 1024
_PHOTO_MAX_BYTES = 10 * 1024 * 1024

#: One-time pairing codes, in memory only: ``code -> {user_id, expires}``. A
#: code that lived in the settings table would be a secret the browser could
#: read back through the settings endpoint, and a restart dropping a pending
#: code costs a re-click, not the pairing.
_PAIRING: dict[str, dict] = {}
_PAIRING_LOCK = threading.Lock()

_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()
_POLLER_STOP = threading.Event()

#: Last poll error, so the settings tab can say why the bot is not listening.
_LAST_ERROR: dict = {"message": "", "at": 0.0}


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def enabled(runtime=None) -> bool:
    """Is the bot master-switch on? Cheap check used to arm the tools/prompt."""
    if runtime is None:
        from db import get_db

        runtime = get_db().all_settings()
    return _truthy(runtime.get("telegram_enabled"))


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def effective_settings(config, runtime=None) -> dict:
    """Settings-DB overrides merged over env defaults."""
    if runtime is None:
        from db import get_db

        runtime = get_db().all_settings()

    def pick(key: str, attr: str, default: str = "") -> str:
        if key in runtime and runtime[key] is not None:
            return str(runtime[key])
        return str(getattr(config, attr, "") or default)

    language = str(runtime.get("response_language", getattr(config, "RESPONSE_LANGUAGE", "en")) or "en")
    engine = pick("telegram_tts_engine", "TELEGRAM_TTS_ENGINE", "espeak").strip().lower()
    if engine not in ALLOWED_TTS_ENGINES:
        engine = "espeak"
    return {
        "telegram_enabled": _truthy(runtime.get("telegram_enabled", getattr(config, "TELEGRAM_ENABLED", False))),
        "telegram_bot_token": str((runtime.get("telegram_bot_token") if runtime.get("telegram_bot_token") is not None
                                   else getattr(config, "TELEGRAM_BOT_TOKEN", "")) or "").strip(),
        "telegram_voice_replies": _truthy(runtime.get(
            "telegram_voice_replies", getattr(config, "TELEGRAM_VOICE_REPLIES", True))),
        "telegram_tts_engine": engine,
        "telegram_tts_voice": pick("telegram_tts_voice", "TELEGRAM_TTS_VOICE").strip(),
        "response_language": language.strip().lower() or "en",
        "configured": bool(pick("telegram_bot_token", "TELEGRAM_BOT_TOKEN").strip()),
    }


def revoke_token(config) -> None:
    """Forget the bot identity completely: stored token gone, pairing gone,
    and the env fallback dead so nothing can resurrect it.

    Clearing the stored row is only half of it - ``effective_settings`` falls
    back to ``config.TELEGRAM_BOT_TOKEN`` (the import-time env), so a token an
    operator once set in ``backend/.env`` would silently come back on the next
    poll and the bot would keep serving. The runtime attribute and the
    environment are cleared too, and every pending pairing code is cancelled,
    because a new bot is a new identity.
    """
    from db import get_db

    get_db().clear_settings("telegram_bot_token")
    unlink_pair()
    cancel_all_pairing_codes()
    key = "TELEGRAM_BOT_TOKEN"
    if key in os.environ:
        del os.environ[key]
    setattr(config, key, "")


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
def redact(text: str, cfg: dict | None = None) -> str:
    """Strip the bot token from anything headed for the model or the log."""
    out = str(text)
    token = str((cfg or {}).get("telegram_bot_token") or "")
    if token:
        out = out.replace(token, "***")
    return out


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------
def _pairing_code() -> str:
    # No vowels, 0/O or 1/l: the code is sent to the bot by hand and read back
    # from a phone, and those are the chars people mis-type.
    alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def generate_pairing_code(config, user_id: str) -> str:
    """Create a one-time code that pairs a Telegram chat with ``user_id``."""
    ttl = int(getattr(config, "TELEGRAM_PAIRING_TTL_SECONDS", 600) or 600)
    with _PAIRING_LOCK:
        now = time.time()
        for code in list(_PAIRING):
            if _PAIRING[code]["expires"] <= now:
                del _PAIRING[code]
        code = _pairing_code()
        _PAIRING[code] = {"user_id": user_id, "expires": now + ttl}
    return code


def pending_pairing_code(user_id: str) -> dict | None:
    """Return the operator's outstanding code, if any (for the settings tab)."""
    with _PAIRING_LOCK:
        now = time.time()
        for code, entry in _PAIRING.items():
            if entry["user_id"] == user_id and entry["expires"] > now:
                return {"code": code, "expires": entry["expires"]}
    return None


def cancel_pairing_codes(user_id: str) -> None:
    with _PAIRING_LOCK:
        for code in [c for c, e in _PAIRING.items() if e["user_id"] == user_id]:
            del _PAIRING[code]


def cancel_all_pairing_codes() -> None:
    with _PAIRING_LOCK:
        _PAIRING.clear()


def redeem_pairing_code(code: str, chat_id: str, chat_username: str = "") -> str | None:
    """Pair ``chat_id`` with the user a code was generated for.

    Returns the user_id on success (single redemption - the code is consumed),
    ``None`` when the code is unknown or expired. This is the only way the bot
    learns which chat it may serve.
    """
    code = (code or "").strip()
    with _PAIRING_LOCK:
        entry = _PAIRING.get(code)
        if not entry or entry["expires"] <= time.time():
            return None
        del _PAIRING[code]
        user_id = entry["user_id"]
    from db import get_db

    db = get_db()
    db.set_setting("telegram_paired_chat_id", str(chat_id))
    db.set_setting("telegram_paired_username", (chat_username or "").strip())
    db.set_setting("telegram_paired_user_id", user_id)
    db.set_setting("telegram_paired_at", str(int(time.time())))
    return user_id


def paired_link(runtime=None) -> dict:
    """The paired chat, read from settings. ``{}`` when nobody is paired."""
    if runtime is None:
        from db import get_db

        runtime = get_db().all_settings()
    chat_id = runtime.get("telegram_paired_chat_id")
    user_id = runtime.get("telegram_paired_user_id")
    if not chat_id or not user_id:
        return {}
    return {
        "chat_id": str(chat_id),
        "user_id": str(user_id),
        "username": str(runtime.get("telegram_paired_username") or ""),
    }


def unlink_pair() -> None:
    from db import get_db

    get_db().clear_settings(*MANAGED_KEYS)


def conv_id_for_chat(chat_id: str, runtime) -> str | None:
    key = f"telegram_conversation_{chat_id}"
    return str(runtime.get(key) or "") if runtime.get(key) else None


def remember_conv_for_chat(chat_id: str, conv_id: str) -> None:
    from db import get_db

    get_db().set_setting(f"telegram_conversation_{chat_id}", conv_id)


#: Pairing redemptions are the only door a stranger has into the bot, so the
#: six-character code is rate-limited per chat. Without a limit, a script that
#: finds the bot handle could brute-force the code in the minutes it is alive;
#: with eight guesses per quarter of an hour it is a losing proposition, and
#: the attempt is silent so a guesser cannot tell it was even counted.
_REDEEM_LIMIT = 8
_REDEEM_WINDOW = 900.0  # 15 minutes
_REDEEM_ATTEMPTS: dict[str, dict] = {}
_REDEEM_LOCK = threading.Lock()


def _redeem_permitted(chat_id: str) -> bool:
    with _REDEEM_LOCK:
        now = time.time()
        entry = _REDEEM_ATTEMPTS.get(chat_id)
        if entry is None or now - entry["start"] >= _REDEEM_WINDOW:
            _REDEEM_ATTEMPTS[chat_id] = {"start": now, "attempts": 0}
            return True
        return entry["attempts"] < _REDEEM_LIMIT


def _note_redeem_failure(chat_id: str) -> None:
    with _REDEEM_LOCK:
        entry = _REDEEM_ATTEMPTS.get(chat_id)
        if entry is None:
            entry = {"start": time.time(), "attempts": 0}
            _REDEEM_ATTEMPTS[chat_id] = entry
        entry["attempts"] += 1


# --------------------------------------------------------------------------
# Telegram HTTP
# --------------------------------------------------------------------------
def bot_request(method: str, token: str, *, timeout: tuple[float, float] = (10.0, 40.0), **kwargs) -> tuple[dict | None, str]:
    """One call to the Bot API. Returns ``(json, error)``; error is ``""`` on success.

    The error is never raised: the poller and the settings tab both call this
    and both need "the network is down / the token is bad" as a value, not an
    exception that takes a thread down.
    """
    try:
        resp = requests.request(
            "POST" if ("json" in kwargs or "files" in kwargs or "data" in kwargs) else "GET",
            f"{_API}/bot{token}/{method}",
            timeout=timeout,
            **kwargs,
        )
    except requests.RequestException as exc:
        return None, f"Telegram unreachable: {type(exc).__name__}"
    try:
        payload = resp.json()
    except ValueError:
        return None, f"Telegram answered {resp.status_code} with non-JSON"
    if not payload.get("ok"):
        desc = str(payload.get("description") or payload.get("error_code") or "unknown error")
        if desc.lower().startswith("conflict"):
            desc += " - another long poller is using this bot token"
        return None, desc
    return payload.get("result"), ""


def validate_token(token: str) -> bool:
    """Bot token shape: `<botid>:<secret>`. Final truth comes from getMe.

    Shaped to BotFather output (a numeric bot id, colon, a base64ish secret of
    ~35 chars). The regex refuses lengths BotFather never produces so a paste
    of the wrong thing is caught here rather than by Telegram's own gateway.
    """
    return bool(re.fullmatch(r"\d{1,12}:[A-Za-z0-9_-]{25,45}", str(token or "").strip()))


def get_me(token: str) -> dict | None:
    result, _err = bot_request("getMe", token)
    return result if isinstance(result, dict) else None


def download_chat_file(token: str, file_id: str, dest: Path) -> Path | None:
    """Resolve a file_id to a path and fetch it to ``dest``.

    The file is streamed to disk with a hard size cap so a malicious or corrupt
    payload cannot exhaust memory or disk; anything past the cap is deleted and
    reported as unreadable rather than half-processed.
    """
    result, err = bot_request("getFile", token, timeout=(10.0, 30.0), params={"file_id": file_id})
    if err or not isinstance(result, dict):
        return None
    file_path = result.get("file_path")
    if not file_path:
        return None
    try:
        with requests.get(f"{_API}/file/bot{token}/{file_path}", timeout=(10.0, 120.0), stream=True) as resp:
            if resp.status_code != 200:
                return None
            size = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MEDIA_MAX_BYTES:
                        dest.unlink(missing_ok=True)
                        return None
                    fh.write(chunk)
        return dest
    except (requests.RequestException, OSError):
        dest.unlink(missing_ok=True)
        return None


def ensure_wav(path: Path) -> Path:
    """Normalise an incoming note to 16 kHz mono WAV for the recogniser.

    faster-whisper decodes most containers itself, but normalising here removes
    the container as a variable; when ffmpeg is absent the file passes through
    untouched and the recogniser does its own resampling.
    """
    ffmpeg = shutil.which("ffmpeg")
    out = path.with_suffix(".wav")
    if not ffmpeg or out == path:
        return path
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-ac", "1", "-ar", "16000", str(out)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size:
            return out
    except (subprocess.SubprocessError, OSError):
        pass
    return path


def transcribe(audio: Path, config, language: str = "") -> str:
    """Transcribe an incoming Telegram voice note with faster-whisper.

    Same virtualenv and helper as SIP; the model can be overridden with
    TELEGRAM_WHISPER_MODEL. An absent setup returns a sentence the operator can
    read rather than raising - hearing is a nice-to-have, the reply is not.
    """
    if not audio.exists() or audio.stat().st_size <= 44:
        return ""
    raw = str(getattr(config, "TELEGRAM_WHISPER_PYTHON", "") or "").strip()
    # `Path("")` is `.`, which exists - same guard as SIP.
    interpreter = Path(raw) if raw else None
    helper = Path(__file__).resolve().parent / "sip_whisper.py"
    if interpreter is None or not interpreter.exists():
        return "(Speech recognition is not set up, so the voice note could not be read. " \
               "Install it with: python3 -m venv .venv/sip-whisper " \
               "&& .venv/sip-whisper/bin/pip install -r backend/tools/sip-whisper-requirements.txt)"
    if not helper.exists():
        return "(The speech recognition helper is missing from this installation.)"
    cmd = [str(interpreter), str(helper), str(audio), "--model",
           str(getattr(config, "TELEGRAM_WHISPER_MODEL", "") or "tiny")]
    if language:
        cmd += ["--language", language]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "(Transcription timed out; the voice note was longer than expected.)"
    if proc.returncode:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "(Could not transcribe the voice note: " + (lines[-1] if lines else "unknown error") + ")"
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------
# replying: text and spoken voice notes
# --------------------------------------------------------------------------
def to_opus_ogg(src: Path, dest: Path) -> Path | None:
    """Convert a synthesized MP3 or WAV into an ogg/opus voice note.

    Telegram's ``sendVoice`` only accepts ogg/opus; espeak writes WAV and edge
    writes MP3. ffmpeg is the one required binary for voice replies; without it
    the bot falls back to a text-only reply rather than failing the message.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ac", "1", "-ar", "48000", "-c:a", "libopus", str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 44:
            return dest
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def send_message(token: str, chat_id: str, text: str) -> tuple[dict | None, str]:
    """Plain-text reply. Returns ``(json, error)``; never raises."""
    return bot_request(
        "sendMessage",
        token,
        timeout=(10.0, 40.0),
        json={"chat_id": chat_id, "text": _clip(text, _TEXT_MAX)},
    )


def _send_path_allowed(path: Path, config) -> str:
    """Resolve ``path`` and return reason it may not be sent, or "" when OK.

    The model picks the path, so the check is the security boundary: only files
    the operator already owns on this host may travel to the paired chat.
    Allowed roots are the service account's home, the APEX data dir (generated
    editor files, visio frames) and /tmp (transient tool output). Files whose
    name contains ``.env``, the settings database itself, and anything under
    /etc or /var are refused outright; a settings or credential file appearing
    at any level is rejected before the read.
    """
    denied_components = (".env", "shadow", "settings.db", "credentials.json", ".aws", ".gnupg")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return "path does not exist"
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        return "not a readable file"
    if resolved.stat().st_size > _SEND_MAX_BYTES:
        return f"file is larger than the {_SEND_MAX_BYTES // (1024 * 1024)} MB send cap"
    low = resolved.as_posix().lower()
    if low.startswith("/etc/") or low.startswith("/var/") or any(
        component.name in ("etc", "var") for component in resolved.parents
    ):
        return "system paths may not be sent"
    parts = {p.lower() for p in resolved.parts}
    if any(any(den in part for den in denied_components) for part in parts):
        return "credential or settings files may not be sent"
    allowed = {
        Path(config.DATA_DIR).resolve(),
        Path(config.DB_PATH).resolve().parent,
        Path.home(),
        Path("/tmp"),
    }
    if not any(_within(root, resolved) for root in allowed):
        return "path is outside the allowed sendable locations"
    return ""


def _within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_signed_url(value: str) -> str:
    """Strip a host/port and the /be rewrite prefix from a signed APEX URL."""
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        value = parsed.path or ""
        if parsed.query:
            value = f"{value}?{parsed.query}"
    if value.startswith("/be"):
        value = value[3:]
    return value


def _is_apex_url(value: str) -> bool:
    return _normalize_signed_url(value).startswith("/api/")


#: Signed APEX file links already delivered this turn: normalized url -> file
#: name. A fresh snapshot (visio emit) and every /api/... link the model types
#: into its reply are delivered automatically; the map is what stops the same
#: file arriving twice when the model ALSO calls telegram_send_file with it.
_DELIVERED: dict[str, str] = {}
_DELIVERED_LOCK = threading.Lock()


def _reset_delivered() -> None:
    with _DELIVERED_LOCK:
        _DELIVERED.clear()


def _deliver_file(config, token: str, chat_id: str, user_id: str, value: str) -> str | None:
    """Deliver one signed file link once; returns the file name, else None.

    Runs from two places: the telegram AgentContext's push_event hook (a visio
    snapshot taken with show=true arrives in the chat the moment it is stored)
    and the reply sanitizer (a /api/... link the model typed instead of calling
    the tool). Both are mechanical - delivery must not depend on the model
    remembering to call telegram_send_file.
    """
    norm = _normalize_signed_url(value)
    with _DELIVERED_LOCK:
        if norm in _DELIVERED:
            return _DELIVERED[norm]
    path, name = _send_target(value, user_id, config)
    if path is None or not token:
        return None
    _result, err = send_file(token, chat_id, value, "", user_id, config)
    if err:
        return None
    with _DELIVERED_LOCK:
        _DELIVERED[norm] = name
    return name


def _attach_telegram_emit(ctx: object, config, token: str, chat_id: str) -> None:
    """Make a turn's mid-tool events deliver files to the paired chat.

    Running in front of ``AgentContext.push_event`` mirrors the browser path:
    the browser opens a snapshot from the ``visio_frame`` event, and a Telegram
    turn delivers it instead. Only file events are acted on; every other event
    keeps flowing to the engine unchanged.
    """
    original = ctx.push_event

    def push(event: dict) -> None:
        original(event)
        try:
            if (
                isinstance(event, dict)
                and event.get("type") == "visio_frame"
                and event.get("url")
            ):
                _deliver_file(config, token, chat_id, str(ctx.user_id), str(event["url"]))
        except Exception:
            pass

    ctx.push_event = push


_REPLY_URL_RE = re.compile(r"(?:https?://[^\s]+?)?/(?:be/)?api/[A-Za-z0-9_.\-/?=&%:+]+")


def _deliver_links_in_reply(config, token: str, chat_id: str, user_id: str, text: str) -> tuple[str, int]:
    """Deliver any signed /api/... link the model typed, and quote it as sent.

    This is the backstop for "it gives me a link": a file whose link appears in
    the reply text (the model echoed its tool result instead of calling
    telegram_send_file, or a visio image_url) is resolved and sent to the chat,
    and the bare link is replaced with a short ``[sent: name]`` so the operator
    knows it arrived. Links that are not APEX file links, or that no longer
    resolve, are left exactly as the model wrote them.
    """
    if not text or not token:
        return text, 0
    delivered = 0

    def repl(match: re.Match) -> str:
        nonlocal delivered
        candidate = match.group(0).rstrip(".,;!?)]}'\">")
        if not _is_apex_url(candidate):
            return match.group(0)
        with _DELIVERED_LOCK:
            already = _DELIVERED.get(_normalize_signed_url(candidate))
        if already:
            return f"[sent: {already}]"
        name = _deliver_file(config, token, chat_id, user_id, candidate)
        if name is None:
            return match.group(0)
        delivered += 1
        return f"[sent: {name}]"

    return _REPLY_URL_RE.sub(repl, text), delivered


def _send_target(value: str, user_id: str, config) -> tuple[Path | None, str]:
    """Turn a filesystem path or a signed APEX file URL into (path, name).

    The model only ever sees the artifacts it generated as signed URLs
    (``/api/editor/download/…``, ``/api/visio/frame/…``, ``/api/files/
    download/…``), never the disk path, so an operator asking for "the file" is
    best answered by resolving that link to the real file and sending it.
    URL-resolved files are already wrapped in their generator's own
    ownership/containment checks; a plain path still has to pass
    ``_send_path_allowed``. Returns ``(None, reason)`` instead of raising.
    """
    raw = (value or "").strip()
    if not raw:
        return None, "path is empty"
    if _is_apex_url(raw):
        try:
            from tools.preview_tools import _PreviewError, _resolve_source
        except Exception:
            return None, "could not resolve that file link"
        try:
            return _resolve_source(_normalize_signed_url(raw), str(user_id or ""), config)
        except _PreviewError as exc:
            return None, exc.message
        except Exception:
            return None, "could not resolve that file link"
    path = Path(raw)
    reason = _send_path_allowed(path, config)
    if reason:
        return None, reason
    resolved = path.expanduser().resolve()
    return resolved, resolved.name


def send_file(token: str, chat_id: str, value: str | Path, caption: str = "", user_id: str = "", config=None) -> tuple[dict | None, str]:
    """Send a local file or signed APEX file link to the paired chat.

    Images under the photo cap go out via ``sendPhoto`` so they render inline
    as pictures in the chat; everything else (or a larger image) is sent as a
    document preserving the original bytes. Returns ``(None, reason)`` instead
    of raising, and the caption is clipped to Telegram's 1024-char cap.
    """
    if config is None:
        return None, "no config"
    target, name = _send_target(str(value), user_id, config)
    if target is None:
        return None, name
    mime, _ = mimetypes.guess_type(str(target))
    as_photo = bool(mime and mime.startswith("image/") and target.stat().st_size <= _PHOTO_MAX_BYTES)
    method = "sendPhoto"
    fkey = "photo"
    if not as_photo:
        method, fkey = "sendDocument", "document"
    data: dict = {"chat_id": chat_id}
    if caption.strip():
        data["caption"] = _clip(caption.strip(), _CAPTION_MAX)
    try:
        with target.open("rb") as fh:
            result, err = bot_request(
                method,
                token,
                timeout=(10.0, 60.0),
                data=data,
                files={fkey: (name, fh, mime or "application/octet-stream")},
            )
        return (result, "") if (result is not None or not err) else (None, err)
    except Exception as exc:
        return None, redact(f"{type(exc).__name__}: {exc}", {})


def send_voice_note(token: str, chat_id: str, text: str, config, cfg: dict) -> tuple[dict | None, str]:
    """Speak ``text`` and send it as a voice note (ogg/opus) with the text as caption.

    Falls back to espeak when edge is selected but off-line, exactly like SIP's
    ``synthesize`` does. Returns ``(None, reason)`` instead of raising; the
    caller then sends the plain text so the operator still gets the answer.
    """
    result, err = bot_request("sendChatAction", token, json={"chat_id": chat_id, "action": "record_voice"})
    if err:
        return None, err
    language = "el" if str(cfg.get("response_language") or "").startswith("el") else "en"

    from tools.sip_tools import edge_voice_for_language, synthesize

    engine = cfg.get("telegram_tts_engine") or "espeak"
    workdir = Path(tempfile.mkdtemp(prefix="apex-tg-"))
    try:
        src = synthesize(
            text=_clip(text, 3000),
            out_wav=workdir / "reply.wav",
            language="el" if engine == "espeak" and language == "el" else "",
            voice=edge_voice_for_language(cfg.get("telegram_tts_voice") or "", language) if engine == "edge" else "",
            engine=engine,
        )
        ogg = to_opus_ogg(src, workdir / "reply.ogg")
        if ogg is None:
            return None, "no ogg/opus encoder (ffmpeg)"
        with ogg.open("rb") as fh:
            result, err = bot_request(
                "sendVoice",
                token,
                timeout=(10.0, 60.0),
                data={"chat_id": chat_id, "caption": _clip(text, _CAPTION_MAX)},
                files={"voice": ("reply.ogg", fh, "audio/ogg")},
            )
        return (result, "") if (result is not None or not err) else (None, err)
    except Exception as exc:  # synthesize raises on missing engines; degrade to text
        return None, redact(f"{type(exc).__name__}: {exc}", cfg)


# --------------------------------------------------------------------------
# one incoming message -> one agent turn
# --------------------------------------------------------------------------
def _message_text(msg: dict) -> str:
    return str(msg.get("text") or msg.get("caption") or "").strip()


def _message_media(msg: dict) -> dict:
    """Return ``{"kind": ..., "file_id": ...}`` for a voice-like message, else {}."""
    for kind in ("voice", "video_note", "audio"):
        media = msg.get(kind)
        if isinstance(media, dict) and media.get("file_id"):
            return {"kind": kind, "file_id": media["file_id"]}
    if isinstance(msg.get("document"), dict) and msg["document"].get("file_id"):
        mime = str(msg["document"].get("mime_type") or "")
        if mime.startswith("audio/") or "opus" in mime or mime.endswith("ogg"):
            return {"kind": "document", "file_id": msg["document"]["file_id"]}
    return {}


def run_message_turn(config, user_id: str, chat_id: str, text: str, username: str = "", spoken_reply: bool = False) -> tuple[str, str]:
    """One agent turn for a Telegram message. Returns ``(report, error)``.

    Mirrors ``tasks.run_task_turn``: same provider selection, memory, skills and
    tool registry, with a per-chat conversation so a thread keeps its context
    across messages. Runs as the browser user who requested the pairing, so the
    operator's own tools and memory are what APEX drives.

    ``spoken_reply`` tells the model the answer will be read aloud as a voice
    note (the operator sent voice); it then keeps the reply to a few spoken
    sentences. Text turns get no such limit: the full answer, tools and all.
    """
    from auth import bearer_for_api, is_subscription_access
    from agent.base import AgentContext
    from agent.factory import build_engine, make_registry
    from db import get_db
    from memory.store import get_memory
    from models.providers import ProviderError, ProviderManager
    from skills.manager import get_skill_manager, route_skill
    from tools.tasks import collect_stream

    db = get_db()
    uid = str(user_id)
    runtime = db.all_settings()
    provider_name = (runtime.get("provider") or "openai").lower()
    engine_name = (runtime.get("engine") or "responses").lower()

    try:
        provider = ProviderManager(
            bearer=bearer_for_api(uid),
            use_oauth_access=is_subscription_access(uid),
            runtime=runtime,
        ).build(provider_name, str(runtime.get("model") or ""))
    except ProviderError as exc:
        return "", f"no model provider available: {exc}"

    memory = None
    if bool(getattr(config, "MEMORY_ENABLED", False)):
        try:
            memory = get_memory()
        except Exception:
            memory = None

    conv_id = conv_id_for_chat(chat_id, runtime)
    conv = db.get_conversation(conv_id) if conv_id else None
    if conv is None or str(conv.get("user_id")) != uid:
        conv = db.create_conversation(uid, title=f"Telegram · {username or chat_id}")
    remember_conv_for_chat(chat_id, conv["id"])

    skill_manager = get_skill_manager()
    skill_name = "general"
    if getattr(config, "AUTO_ROUTE_FROM_GENERAL", True):
        try:
            skill_name = route_skill(text, skill_manager.all(), provider, fallback="general")
        except Exception:
            skill_name = "general"
    skill_obj = skill_manager.select(skill_name)

    user_row = db.get_user(uid) or {}
    system_prompt = get_skill_manager().build_system_prompt(
        skill_name,
        voice_mode=False,
        user_name=user_row.get("name") or "",
        response_language=runtime.get("response_language") or getattr(config, "RESPONSE_LANGUAGE", "en"),
        extra="",
        telegram_enabled=True,
    )
    system_prompt = system_prompt.rstrip() + (
        "\n\nThis request reached you over Telegram, from the operator's own "
        "account. Answer plainly and act with the tools; the reply below is the "
        "only channel.\n"
        "The operator CANNOT see any browser, terminal window or file you open: "
        "the text of your reply is the ONLY thing that reaches them. Whatever "
        "you inspect or run, ALWAYS put the actual result in your reply. Use "
        "headless tools (run_shell) over visible windows when the command is "
        "non-interactive, and when a command must run in a terminal, reproduce "
        "its captured output in your message. NEVER say 'I opened a terminal', "
        "'the output is in the terminal', 'watch it there' or similar - nobody "
        "is looking anywhere but here. Never ask whether the operator can see "
        "something.\n"
        "When the operator asks for a FILE - a screenshot, a report, 'save that to a "
        "file and send it', 'send me test.docx', an image you can see - actually "
        "attach it with telegram_send_file (give the on-disk path or its /api/... "
        "link) and say you sent it. NEVER answer with a web link like 'open it here' "
        "or 'available at http://...': a link that only opens in the browser is not "
        "a delivered file on Telegram, and any /api/... link you DO type into your "
        "reply is delivered as a real file automatically instead of being left as "
        "a link. A visio snapshot you take with show=true is sent to the chat as a "
        "photo by itself - just report what you see, never paste its link. To "
        "produce a file on request, save command output or generated content to "
        "one (for example run df, then write the output to /tmp/df.txt and send "
        "that file).\n"
        + (
            "This message was a VOICE NOTE, so your reply will be read aloud as "
            "a spoken voice note. Keep it to 1-3 short plain sentences; if you "
            "ran a command or read a number, report it in words (for example "
            "'192.168.1.44 is free'). No tables, code blocks or paths."
            if spoken_reply
            else "This message was plain TEXT, so the reply comes back as text: "
                 "answer fully and run whatever command is asked, tools included. "
                 "A command's output belongs IN the reply, quoted or summarised. "
                 "Keep it readable on a phone, but never truncate a real answer."
        )
    )

    window = int(getattr(config, "TASKS_HISTORY_WINDOW", 20) or 20)
    history = [
        {"role": message["role"], "content": message["content"]}
        for message in db.list_messages(conv["id"])[-window:]
        if message["role"] in ("user", "assistant") and message.get("content")
    ]

    message_text = text or "(voice note)"
    db.add_message(conv["id"], "user", message_text, {"telegram": True, "chat_id": chat_id})
    # The engine is fed ctx.history verbatim, so THIS message must be in it -
    # otherwise the model answers to whatever the last stored turn was (it
    # "re-runs the last command") instead of to what was just said.
    history.append({"role": "user", "content": message_text})

    ctx = AgentContext(
        user_id=uid,
        conversation_id=conv["id"],
        system_prompt=system_prompt,
        history=history,
        provider=provider,
        provider_kind=provider_name,
        engine_name=engine_name,
        skill_name=skill_name,
        tools=make_registry(memory=memory),
        skill_tools=list(getattr(skill_obj, "tools", []) or []),
        require_tool=bool(getattr(skill_obj, "require_tool", False)),
        exclude_tools=list(getattr(skill_obj, "exclude_tools", ()) or ()),
        memory=memory,
        runtime=runtime,
        voice_mode=False,
        user_name=user_row.get("name") or "",
    )

    # Deliver files by construction, not by the model's memory: a visio snapshot
    # taken with show=true lands in the chat through the same event the browser
    # would open, and a signed link the model types into its reply is sent as a
    # real file and quoted as sent - never left as a link the phone cannot open.
    _reset_delivered()
    token = str((effective_settings(config).get("telegram_bot_token") or ""))
    _attach_telegram_emit(ctx, config, token, chat_id)
    report, error = collect_stream(build_engine(engine_name, ctx), int(getattr(config, "TASKS_TIMEOUT_SECONDS", 600) or 600))
    if report:
        report, _ = _deliver_links_in_reply(config, token, chat_id, uid, report)
    db.add_message(
        conv["id"], "assistant", report or "", {"telegram": True, "chat_id": chat_id, **({"error": error} if error else {})},
    )
    if error:
        return "", error
    if not report.strip():
        return "", "the model returned nothing"
    return report, ""


def _reply(config, cfg: dict, token: str, chat_id: str, text: str, wants_voice: bool = False) -> None:
    """Send the turn's reply back to the paired chat; degrade text-ward, never fail.

    Voice is a reply to the *operator's* voice: a spoken voice note is only
    produced when the incoming message was itself voice, and only while the
    ``telegram_voice_replies`` toggle is on. A text message always gets a plain
    text reply, so a typed command is answered fast and in full instead of being
    slowed down (and cut short) by synthesis.
    """
    if not text.strip():
        return
    if wants_voice and cfg.get("telegram_voice_replies"):
        _result, _err = send_voice_note(token, chat_id, text, config, cfg)
        if _result is not None:
            return
        # Voice failed for some reason: send the text so the answer still arrives.
    send_message(token, chat_id, text)


def _chat_action(token: str, chat_id: str, action: str = "typing") -> None:
    """Inform the Telegram UI that the bot is busy ("typing…"), best effort.

    A turn can take many seconds (model round trip plus tools) and the UI shows
    nothing while it runs; without this the operator sees the bot as idle and
    may re-send the message. Never fails the turn.
    """
    try:
        bot_request("sendChatAction", token, timeout=(5.0, 10.0), json={"chat_id": chat_id, "action": action})
    except Exception:
        pass


def _handle_message(config, cfg: dict, msg: dict) -> None:
    token = cfg["telegram_bot_token"]
    if not token:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    # Only a private chat is ever answered or paired. Groups leak the pairing
    # hint and, worse, let someone drop a code into a chat the bot can read.
    if str(chat.get("type") or "") != "private":
        return
    sender = msg.get("from") or {}
    username = str(sender.get("username") or sender.get("first_name") or "")
    text = _message_text(msg)

    link = paired_link()
    # The pairing code is redeemable from any private chat, before pairing
    # exists. An unpaired chat is otherwise ignored: answering a stranger tells
    # them the bot is live and invites guessing, so there is no "who are you"
    # reply. Redemption attempts are rate-limited so the six-character code
    # cannot be brute-forced while it is alive.
    if not link:
        if text == "/start":
            send_message(token, chat_id, "This bot is APEX, paired through the APEX settings tab. "
                                        "Send the pairing code shown there to continue.")
            return
        if not _redeem_permitted(chat_id):
            return
        if redeem_pairing_code(text, chat_id, username) is not None:
            _REDEEM_ATTEMPTS.pop(chat_id, None)
            send_message(token, chat_id, "Paired. You can now talk to APEX here - "
                                        "text or voice notes, you will get the answer back.")
            return
        _note_redeem_failure(chat_id)
        return

    if chat_id != link["chat_id"]:
        return

    media = _message_media(msg)
    was_voice = bool(media)
    if media:
        audio = Path(tempfile.gettempdir()) / f"apex-tg-{secrets.token_hex(6)}.ogg"
        try:
            downloaded = download_chat_file(token, media["file_id"], audio)
            if downloaded is not None:
                language = "el" if str(cfg.get("response_language") or "").startswith("el") else ""
                transcript = transcribe(ensure_wav(downloaded), config, language)
                if transcript:
                    text = f"{text} {transcript}".strip() if text else transcript
        finally:
            for leftover in (audio, audio.with_suffix(".wav")):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
    if not text.strip():
        send_message(token, chat_id, "(I could not read that voice note - the speech recognizer is not working.)")
        return

    _chat_action(token, chat_id, "typing")
    report, error = run_message_turn(
        config, link["user_id"], chat_id, text, username,
        spoken_reply=bool(was_voice and cfg.get("telegram_voice_replies")),
    )
    if error:
        send_message(token, chat_id, f"I ran into a problem: {error[:800]}")
        return
    _reply(config, cfg, token, chat_id, report, wants_voice=bool(was_voice))


def _poll_loop(config) -> None:
    offset = 0
    while not _POLLER_STOP.is_set():
        try:
            cfg = effective_settings(config)
            if not cfg["telegram_enabled"] or not cfg["telegram_bot_token"]:
                _POLLER_STOP.wait(3)
                offset = 0
                continue
            result, err = bot_request(
                "getUpdates",
                cfg["telegram_bot_token"],
                timeout=(5.0, float(getattr(config, "TELEGRAM_POLL_TIMEOUT_SECONDS", 25) or 25) + 15.0),
                params={"offset": offset, "timeout": int(getattr(config, "TELEGRAM_POLL_TIMEOUT_SECONDS", 25) or 25)},
            )
            if err:
                _LAST_ERROR["message"] = err
                _LAST_ERROR["at"] = time.time()
                _POLLER_STOP.wait(5)
                continue
            _LAST_ERROR["message"] = ""
            _LAST_ERROR["at"] = 0.0
            for update in result or []:
                update_id = int(update.get("update_id") or 0)
                msg = update.get("message")
                if isinstance(msg, dict):
                    try:
                        _handle_message(config, cfg, msg)
                    except Exception as exc:  # never let one message kill the loop
                        import logging

                        logging.getLogger("apex.telegram").warning("telegram message handling failed: %s", redact(str(exc), cfg))
                offset = max(offset, update_id + 1)
        except Exception as exc:  # noqa: BLE001 - the poller must never die
            try:
                safe_cfg = cfg
            except UnboundLocalError:
                safe_cfg = {}
            _LAST_ERROR["message"] = redact(f"{type(exc).__name__}: {exc}", safe_cfg)
            _LAST_ERROR["at"] = time.time()
            _POLLER_STOP.wait(5)


def ensure_poller(config) -> None:
    """Start the single long-poll thread. Idempotent; safe from any thread."""
    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return
        _POLLER_STOP.clear()
        _POLLER = threading.Thread(
            target=_poll_loop, args=(config,), daemon=True, name="apex-telegram-poller"
        )
        _POLLER.start()


def poller_health() -> dict:
    running = _POLLER is not None and _POLLER.is_alive()
    return {
        "running": running,
        "last_error": _LAST_ERROR["message"],
        "last_error_at": _LAST_ERROR["at"],
    }


# --------------------------------------------------------------------------
# model-facing tools
# --------------------------------------------------------------------------
def telegram_status_report(config) -> str:
    """Compact human/ model-readable status."""
    cfg = effective_settings(config)
    link = paired_link()
    if not cfg["telegram_enabled"]:
        return "Telegram is turned off (setting telegram_enabled)."
    if not cfg["telegram_bot_token"]:
        return "Telegram is enabled but no bot token is stored - pair it in the settings tab."
    if not link:
        return "Telegram is enabled with a bot token, but no chat is paired yet. Tell the operator to generate a pairing code in the settings tab and send it to the bot."
    voice = "on" if cfg["telegram_voice_replies"] else "off"
    return (f"Telegram is live. Messages from the paired chat run as the operator and replies are sent "
            f"back there as text; when the operator sends a voice note, APEX reads it and answers "
            f"with a spoken voice note (voice replies {voice}, {cfg['telegram_tts_engine']} voice, "
            f"{cfg['response_language']} language).")


def telegram_send(config, ctx, message: str, voice: bool = True) -> str:
    """Message the operator on their paired Telegram chat. ``voice`` speaks it."""
    if not ctx.user_id:
        return "Sign in to use Telegram."
    cfg = effective_settings(config)
    link = paired_link()
    if not cfg["telegram_enabled"]:
        return "Telegram is turned off in the settings; turn on telegram_enabled to message the operator."
    token = cfg["telegram_bot_token"]
    if not token or not link:
        return "The Telegram bot has no paired chat yet; the operator must generate a pairing code in the settings tab and send it to the bot."
    text = str(message or "").strip()
    if not text:
        return "Nothing to send: provide a message."
    if voice:
        _result, _err = send_voice_note(token, link["chat_id"], text, config, cfg)
        if _result is not None:
            return "Sent the operator a Telegram voice note."
    _result, err = send_message(token, link["chat_id"], text)
    if err:
        return f"Could not message the operator on Telegram: {redact(err, cfg)}"
    return "Messaged the operator on Telegram."


def telegram_send_file(config, ctx, path: str, caption: str = "") -> str:
    """Send a local file or image from this host to the operator's paired chat."""
    if not ctx.user_id:
        return "Sign in to use Telegram."
    cfg = effective_settings(config)
    link = paired_link()
    if not cfg["telegram_enabled"]:
        return "Telegram is turned off in the settings; turn on telegram_enabled to send files."
    token = cfg["telegram_bot_token"]
    if not token or not link:
        return "The Telegram bot has no paired chat yet; the operator must generate a pairing code in the settings tab and send it to the bot."
    target = str(path or "").strip()
    if not target:
        return "Nothing to send: provide a path."
    if _is_apex_url(target):
        # A snapshot is already delivered by its own event; sending it again
        # would drop a second copy into the chat.
        with _DELIVERED_LOCK:
            already = _DELIVERED.get(_normalize_signed_url(target))
        if already:
            return f"Already sent {already} - it is in the chat. No need to send it again."
    result, err = send_file(token, link["chat_id"], target, caption, ctx.user_id, config)
    if err:
        return f"Could not send that file: {redact(err, cfg)}"
    _resolved, name = _send_target(target, ctx.user_id, config)
    with _DELIVERED_LOCK:
        _DELIVERED.setdefault(_normalize_signed_url(target), name or Path(target).name)
    return f"Sent {(name or Path(target).name)} to the operator on Telegram."


def build_telegram_tools(config) -> list[Tool]:
    """The telegram tools, bound to this deployment's config.

    Registered unconditionally like SIP; every handler re-reads the settings, so
    enabling it in the tab takes effect on the next turn, and disabling it
    makes both tools answer "turned off" instead of vanishing.
    """

    def t_telegram_send(args: dict, ctx) -> str:
        if not ctx.user_id:
            return "Sign in to use Telegram."
        try:
            return telegram_send(
                config, ctx, str(args.get("message") or ""), bool(args.get("voice", True))
            )
        except Exception as exc:
            return f"Telegram error: {redact(str(exc), None)}"

    def t_telegram_send_file(args: dict, ctx) -> str:
        if not ctx.user_id:
            return "Sign in to use Telegram."
        try:
            return telegram_send_file(
                config, ctx,
                str(args.get("path") or ""),
                str(args.get("caption") or ""),
            )
        except Exception as exc:
            return f"Telegram error: {redact(str(exc), None)}"

    def t_telegram_status(args: dict, ctx) -> str:
        try:
            return telegram_status_report(config)
        except Exception as exc:
            return f"Telegram error: {redact(str(exc), None)}"

    return [
        Tool(
            "telegram_send",
            "Message the operator on their paired Telegram chat, in text or as a spoken "
            "voice note (ogg/opus, TTS in the configured response language). Use this to "
            "tell the operator something without them looking at the app - a finished job, "
            "an alarm, an answer they asked to be sent to their phone.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What to say. For a voice "
                                 "note keep it short - it will be read aloud."},
                    "voice": {"type": "boolean", "description": "Send a spoken voice note "
                                "(default true). false sends plain text."},
                },
                "required": ["message"],
            },
            t_telegram_send,
        ),
        Tool(
            "telegram_status",
            "Report whether the Telegram bot is enabled, has a paired chat, and what voice "
            "and language its replies use.",
            {"type": "object", "properties": {}},
            t_telegram_status,
        ),
        Tool(
            "telegram_send_file",
            "Send a local file, image or signed APEX file link to the operator's paired "
            "Telegram chat. Use this whenever the operator asked for a FILE to arrive on "
            "their phone - a screenshot, a generated Word/Excel document, the output of a "
            "command saved to a file, a downloaded file, a photo. Pass the absolute path "
            "on disk (e.g. /tmp/df.txt, ~/Documents/test.docx) OR the /api/... download "
            "link of a file a tool just produced (an editor, visio or file-search link), "
            "plus an optional short caption. Images arrive as pictures, everything else "
            "as a real document attachment - never reply with a web link instead of "
            "calling this tool.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute filesystem path "
                               "or /api/... file link of what to send."},
                    "caption": {"type": "string", "description": "Optional short caption "
                                 "(kept under 1024 chars)."},
                },
                "required": ["path"],
            },
            t_telegram_send_file,
        ),
    ]