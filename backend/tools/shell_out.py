"""On-screen shell output: capture run_shell results into user-bound text files
and serve them through signed download links the window manager can preview.

When ``run_shell`` is asked to "show" its output, this module writes the command
output to ``$DATA_DIR/generated/shell/<user_id>/`` and returns a signed link.
The preview renderer resolves the same token (text kind) and the frontend opens
it in a floating window; the download route serves the file as an attachment.

The file is tied to one user, guarded with a fingerprint, viewed/tamper-checked
through itsdangerous like the editor download links, and cleaned up after TTL.
"""
from __future__ import annotations

import mimetypes
import os
import time
import uuid
from pathlib import Path

from flask import jsonify, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from public_urls import public_url

SHELL_SALT = "apex-shell-out-v1"
# Sanitized, keep the extension so the preview renderer can classify the file.
_EXT = ".txt"


def _fingerprint(info: os.stat_result) -> list:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def signer(config):
    key = getattr(config, "SECRET_KEY", "dev-change-me")
    return URLSafeTimedSerializer(key, salt=SHELL_SALT)


def _out_base(config) -> Path:
    base = Path(getattr(config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    return (base / "generated" / "shell").resolve()


def _cleanup_old_files(directory: Path, ttl_seconds: int):
    if not directory.exists():
        return
    cutoff = time.time() - ttl_seconds
    for item in directory.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass


def save_shell_output(config, user_id: str, command: str, text: str) -> tuple[str, str]:
    """Write ``text`` to a user-bound .txt file and return (download_url, filename)."""
    if not user_id:
        raise ValueError("Sign in before capturing shell output.")
    directory = _out_base(config) / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    ttl = getattr(config, "SHELL_OUT_TTL_SECONDS", 3600)
    _cleanup_old_files(directory, ttl)

    stem = "".join(ch for ch in (command.splitlines() or [command])[0] if ch.isalnum() or ch in "-_.")[:28]
    filename = f"shell_{stem or 'output'}_{uuid.uuid4().hex[:8]}{_EXT}"
    path = directory / filename
    content = str(text).rstrip()
    if content:
        path.write_text(content + "\n", encoding="utf-8")
    else:
        path.write_text("(no output)\n", encoding="utf-8")

    info = path.stat()
    token = signer(config).dumps(
        {
            "user": str(user_id),
            "path": str(path),
            "fingerprint": _fingerprint(info),
            "filename": filename,
            "command": str(command),
        }
    )
    url = public_url(config, f"/api/shell/download/{token}")
    return url, filename


def register_shell_routes(app, require_user, config):
    @app.get("/api/shell/download/<token>")
    def shell_download(token):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to download this file."}), 401
        ttl = getattr(config, "SHELL_OUT_TTL_SECONDS", 3600)
        try:
            ticket = signer(config).loads(token, max_age=ttl)
        except SignatureExpired:
            return jsonify({"error": "Download link expired. Run the command again."}), 410
        except BadSignature:
            return jsonify({"error": "Invalid download link."}), 404
        if not isinstance(ticket, dict) or ticket.get("user") != str(user["id"]):
            return jsonify({"error": "This download link belongs to a different user."}), 403
        try:
            path = Path(ticket["path"]).resolve()
            base_dir = _out_base(config)
            if not path.is_relative_to(base_dir) or not path.is_file():
                return jsonify({"error": "File is unavailable."}), 404
            info = path.stat()
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify({"error": "File is unavailable."}), 404
        if _fingerprint(info) != ticket.get("fingerprint"):
            return jsonify({"error": "File changed. Run the command again."}), 409
        try:
            download_name = ticket.get("filename") or path.name
            mimetype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
            response = send_file(
                path, as_attachment=True, download_name=download_name,
                mimetype=mimetype, conditional=False, max_age=0,
            )
        except Exception:
            return jsonify({"error": "Could not send file."}), 500
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app