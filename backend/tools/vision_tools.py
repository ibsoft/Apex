"""Images attached to a chat turn.

The operator can attach images to a message. Depending on the active chat model
that either goes straight to the model as a multimodal message, or through the
VISIO vision model as a text description. The decision lives in
`app.py::chat`; this module owns the bytes and the capability check.

Images are per-turn: kept under ``DATA_DIR/chat_images/<user>/`` with a short
TTL and never written into the operator's own folders. The file name is a random
token that is *not* signed, because it never needs to leave the owner's
directory: resolution is scoped to the session user, so another user's token
addresses a file this caller cannot see and answers exactly like a token that
does not exist.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_TURN = 4

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_EXT_MIME = {ext: mime for mime, ext in _MIME_EXT.items()}

_USER_DIR_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_KEY_RE = re.compile(r"[0-9a-f]{32}")


def _root(config) -> Path:
    root = Path(getattr(config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data")) / "chat_images"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _user_dir(config, user_id) -> Path | None:
    name = str(user_id or "")
    if not _USER_DIR_RE.fullmatch(name):
        return None
    return _root(config) / name


def _ttl(config) -> int:
    return int(getattr(config, "CHAT_IMAGE_TTL_SECONDS", 3600) or 3600)


def _user_max(config) -> int:
    return int(getattr(config, "CHAT_IMAGES_USER_MAX", 20) or 20)


def _sweep(directory: Path, ttl: int, keep: int) -> None:
    try:
        entries = sorted(
            (p for p in directory.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    cutoff = time.time() - ttl
    fresh: list[Path] = []
    for path in entries:
        try:
            expired = path.stat().st_mtime < cutoff
        except OSError:
            continue
        if expired:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            fresh.append(path)
    for path in fresh[: max(0, len(fresh) + 1 - keep)]:
        try:
            path.unlink()
        except OSError:
            pass


def store_image(config, user_id: str, data: bytes, mime: str) -> str:
    """Write one attached image and return its token. Raises ValueError on a
    mime type this feature does not accept."""
    ext = _MIME_EXT.get(str(mime or "").lower())
    if ext is None:
        raise ValueError("Only PNG, JPEG, WebP and GIF images are supported.")
    directory = _user_dir(config, user_id)
    if directory is None:
        raise ValueError("This account cannot hold image attachments.")
    directory.mkdir(parents=True, exist_ok=True)
    _sweep(directory, _ttl(config), _user_max(config))
    token = uuid.uuid4().hex
    target = directory / f"{token}{ext}"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return token


def read_image(config, user_id: str, token: str):
    """The bytes and mime of one attachment, or None if it is not this user's,
    no longer fresh, or not a real token."""
    if not _KEY_RE.fullmatch(str(token or "")):
        return None
    directory = _user_dir(config, user_id)
    if directory is None:
        return None
    ttl = _ttl(config)
    try:
        candidates = list(directory.glob(f"{token}.*"))
    except OSError:
        return None
    for target in candidates:
        mime = _EXT_MIME.get(target.suffix)
        if mime is None:
            continue
        try:
            resolved = target.resolve()
            if not resolved.is_relative_to(directory.resolve()):
                continue
            if time.time() - resolved.stat().st_mtime > ttl:
                resolved.unlink(missing_ok=True)
                continue
            return resolved.read_bytes(), mime
        except OSError:
            continue
    return None


def is_vision_capable(model: str, patterns) -> bool:
    """Whether this chat model can accept images directly.

    Matching is a case-insensitive substring, so a versioned id
    (``gpt-4o-mini-2024-...``) and a tagged Ollama name
    (``llama3.2-vision:11b``) both match their pattern. A false negative only
    costs the fallback description path, never correctness."""
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(str(p).strip().lower() in name for p in (patterns or []) if str(p).strip())


def _clean_name(name) -> str:
    text = re.sub(r"[\x00-\x1f]+", " ", str(name or ""))
    return text.strip()[:120]


def resolve_attachments(config, user_id: str, raw) -> list[tuple[str, str, bytes]]:
    """Turn the chat payload's `images` field into (name, mime, bytes).

    Accepts a token string or an object ``{token, name}``; anything that does
    not resolve to this user's stored image is dropped rather than failing the
    whole turn."""
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str, bytes]] = []
    for entry in raw[:MAX_IMAGES_PER_TURN]:
        if isinstance(entry, str):
            token, name = entry, ""
        elif isinstance(entry, dict):
            token, name = entry.get("token"), entry.get("name")
        else:
            continue
        found = read_image(config, user_id, token)
        if found is None:
            continue
        data, mime = found
        out.append((_clean_name(name), mime, data))
    return out


def image_content_parts(text: str, images: list[tuple[str, str, bytes]]) -> list[dict]:
    """A chat-completions content array: the message text plus one image part
    per attachment. The provider turns these into whatever its API wants."""
    import base64

    parts: list[dict] = [{"type": "text", "text": text or "Describe the attached image."}]
    for _, mime, data in images:
        encoded = base64.b64encode(data).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return parts


def describe_chat_images(config, runtime, images, question: str):
    """Fallback when the chat model cannot see: ask the VISIO vision model.

    Returns ``(text, None)`` on success or ``(None, reason)`` when no vision
    model is available or it fails. The caller injects the text (or the honest
    reason) into the turn so the model never pretends to have seen the image."""
    from tools.visio_tools import describe_frame, effective_settings

    settings = effective_settings(config, runtime)
    if not settings["visio_model"]:
        return None, "no vision model is configured (Settings > VISIO)"
    if settings["visio_provider"] not in {"openai", "ollama"}:
        return None, "the VISIO provider must be OpenAI or Ollama (Settings > VISIO)"
    question = (question or "Describe the image in detail.").strip()[:4000]
    described: list[str] = []
    for name, _mime, data in images:
        try:
            description = describe_frame(config, settings, data, question)
        except ValueError as exc:
            return None, f"the vision model could not read it ({exc})"
        except Exception:
            return None, "the vision model could not read it (provider error)"
        label = f"{name}: " if name else ""
        described.append(f"{label}{description}")
    return "\n\n".join(described), None


def register_vision_routes(app, require_user, config):
    """``POST /api/vision/upload`` stores one image and returns its token."""
    from flask import jsonify, request

    @app.post("/api/vision/upload")
    def vision_upload():
        user = require_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        file = request.files.get("image")
        if file is None:
            return jsonify({"error": "no image provided"}), 400
        mime = (file.mimetype or "").lower()
        if mime not in _MIME_EXT:
            return jsonify({"error": "Only PNG, JPEG, WebP and GIF images are supported."}), 415
        data = file.read(MAX_IMAGE_BYTES + 1)
        if not data:
            return jsonify({"error": "empty image"}), 400
        if len(data) > MAX_IMAGE_BYTES:
            return jsonify({"error": "Image exceeds the 8 MB limit."}), 413
        try:
            token = store_image(config, user["id"], data, mime)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "token": token, "name": _clean_name(file.filename), "mime": mime})
