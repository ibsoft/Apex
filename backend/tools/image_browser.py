"""Image browser: search the internet or a configured local directory for images.

Used by the voice-controlled centre preview gallery.
"""
from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path

from flask import jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from public_urls import public_url

IMAGE_SALT = "apex-image-browser-v1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
LINK_SECONDS = 3600


def _images_root(config):
    path = getattr(config, "IMAGES_DIR", "")
    if not path:
        return None
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return None
    return root


def _fingerprint(info: os.stat_result) -> list:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _signer(config):
    return URLSafeTimedSerializer(getattr(config, "SECRET_KEY", "dev-change-me"), salt=IMAGE_SALT)


def _open_regular(path: Path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An absolute file path is required.")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular files can be served.")
        handle = os.fdopen(file_descriptor, "rb")
    except Exception:
        os.close(file_descriptor)
        raise
    return handle, info


def _web_image_search(query: str, limit: int):
    try:
        from ddgs import DDGS
    except ImportError:
        return {"error": "Web image search requires `ddgs` (DuckDuckGo search)."}
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=limit))
        images = []
        for r in results:
            url = r.get("image") or r.get("url") or r.get("thumbnail")
            title = r.get("title", "Image")
            if url:
                images.append({"name": title, "path": url, "url": url})
        return {"images": images, "query": query, "count": len(images), "source": "web"}
    except Exception as exc:
        return {"error": f"Web image search failed: {exc}"}


def _local_image_search(config, query: str, limit: int):
    root = _images_root(config)
    if root is None:
        return {"error": "Local image browser is not configured. Set IMAGES_DIR in backend/.env."}
    query_lower = query.strip().lower()
    matches = []
    try:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                if query_lower and query_lower not in name.lower():
                    continue
                path = Path(dirpath) / name
                try:
                    path = path.resolve()
                    if not path.is_relative_to(root):
                        continue
                    handle, info = _open_regular(path)
                    handle.close()
                    token = _signer(config).dumps(
                        {"user": None, "path": str(path), "fingerprint": _fingerprint(info)}
                    )
                    matches.append({
                        "name": name,
                        "path": str(path),
                        "url": public_url(config, f"/api/images/file/{token}"),
                    })
                    if len(matches) >= limit:
                        break
                except (OSError, ValueError):
                    continue
            if len(matches) >= limit:
                break
    except OSError as exc:
        return {"error": f"Could not read image directory: {exc}"}
    return {"images": matches, "query": query, "count": len(matches), "source": "local"}


def register_image_routes(app, require_user, config):
    @app.get("/api/images/web_search")
    def web_search_images():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to search images."}), 401
        query = (request.args.get("query") or "").strip()
        if not query:
            return jsonify({"error": "Please provide a search query."}), 400
        try:
            limit = max(1, min(int(request.args.get("limit", 8)), 20))
        except (TypeError, ValueError):
            limit = 8
        return jsonify(_web_image_search(query, limit))

    @app.get("/api/images/local_search")
    def local_search_images():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to browse local images."}), 401
        query = (request.args.get("query") or "").strip()
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
        except (TypeError, ValueError):
            limit = 50
        return jsonify(_local_image_search(config, query, limit))

    @app.get("/api/images/file/<token>")
    def serve_image(token):
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to view this image."}), 401
        try:
            ticket = _signer(config).loads(token, max_age=LINK_SECONDS)
        except SignatureExpired:
            return jsonify({"error": "Image link expired. Search again."}), 410
        except BadSignature:
            return jsonify({"error": "Invalid image link."}), 404
        if not isinstance(ticket, dict) or ticket.get("user") not in (None, str(user["id"])):
            return jsonify({"error": "This image link belongs to a different user."}), 403
        try:
            path = Path(ticket["path"]).resolve()
            root = _images_root(config)
            if root is None or not path.is_relative_to(root):
                return jsonify({"error": "Invalid file location."}), 403
            if not path.is_file():
                return jsonify({"error": "Image is unavailable."}), 404
            info = path.stat()
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify({"error": "Image is unavailable."}), 404
        if _fingerprint(info) != ticket.get("fingerprint"):
            return jsonify({"error": "Image changed. Search again."}), 409
        try:
            mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            response = send_file(path, mimetype=mimetype, conditional=False, max_age=0)
        except Exception:
            return jsonify({"error": "Could not send image."}), 500
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
