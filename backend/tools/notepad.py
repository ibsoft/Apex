"""Authenticated storage for the built-in WYSIWYG Notepad app."""
from __future__ import annotations

import html
import hashlib
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from flask import jsonify, request, send_file

MAX_CONTENT_BYTES = 2 * 1024 * 1024
CONTENT_START = "<!-- APEX_NOTEPAD_CONTENT_START -->"
CONTENT_END = "<!-- APEX_NOTEPAD_CONTENT_END -->"
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "hr", "i", "li", "ol", "p", "pre", "span", "strong", "u", "ul", "s", "strike", "sub", "sup",
}
VOID_TAGS = {"br", "hr"}
STYLE_PROPERTIES = {"text-align", "color", "background-color", "font-size", "font-family", "font-weight", "font-style", "text-decoration", "margin-left", "white-space"}


def _safe_part(value: object, fallback: str) -> str:
    text = re.sub(r"[^\w .()-]+", "-", str(value or ""), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return (text[:120] or fallback)


def _document_name(value: object, title: object = "Untitled") -> str:
    source = str(value or "").strip()
    if source:
        if Path(source).name != source or "/" in source or "\\" in source or "\x00" in source:
            raise ValueError("Invalid document name.")
        source = re.sub(r"\.html?$", "", source, flags=re.IGNORECASE)
    return _safe_part(source or title, "Untitled") + ".html"


def _documents_root(config, user_id: str) -> Path:
    configured = str(getattr(config, "NOTEPAD_DOCUMENTS_DIR", "") or "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / "Documents" / "APEX Notepad"
    base = base.resolve()
    user_key = _safe_part(user_id, "user")
    if user_key != user_id:
        user_key += "-" + hashlib.sha256(user_id.encode()).hexdigest()[:16]
    root = (base / user_key).resolve()
    if not root.is_relative_to(base):
        raise ValueError("Invalid document directory.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path_for(root: Path, name: str) -> Path:
    candidate = root / _document_name(name)
    if candidate.is_symlink():
        raise ValueError("Invalid document path.")
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Invalid document name.")
    return candidate


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return
        clean = []
        for key, value in attrs:
            key, value = key.lower(), str(value or "")
            if tag == "a" and key == "href" and re.match(r"^(https?://|mailto:|#)", value, re.I):
                clean.append(("href", value))
            elif key == "style":
                declarations = []
                for declaration in value.split(";"):
                    prop, sep, val = declaration.partition(":")
                    prop, val = prop.strip().lower(), val.strip()
                    if sep and prop in STYLE_PROPERTIES and not re.search(r"url|expression|javascript", val, re.I):
                        declarations.append(f"{prop}:{val}")
                if declarations:
                    clean.append(("style", ";".join(declarations)))
        rendered = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in clean)
        self.parts.append(f"<{tag}{rendered}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(html.escape(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")


def _sanitize(fragment: str) -> str:
    parser = _Sanitizer()
    parser.feed(fragment)
    parser.close()
    return "".join(parser.parts)


def _standalone(title: str, content: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apex-notepad-title" content="{html.escape(title, quote=True)}"><title>{safe_title}</title>
<style>
:root{{--cyan:#00e5ff;--gold:#f5a623;--ink:#eaf4ff;--dim:#aabfd7;--panel:#08101d}}
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;padding:42px 18px;background:radial-gradient(circle at 50% 0,rgba(0,229,255,.12),transparent 38%),#050a13;color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:820px;min-height:calc(100vh - 84px);margin:auto;padding:52px 58px;background:rgba(247,249,252,.98);color:#172131;border:1px solid rgba(0,229,255,.22);border-radius:6px;box-shadow:0 18px 70px rgba(0,0,0,.55)}}
h1{{color:#10263b;font-size:2em;border-bottom:2px solid #00a8bd;padding-bottom:.18em}} h2{{color:#173a55;font-size:1.45em}} blockquote{{margin-left:0;padding-left:14px;border-left:3px solid var(--gold);color:#4b5d70}} a{{color:#007f95}} code,pre{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}} img{{max-width:100%}}
@media print{{body{{padding:0;background:#fff}}main{{box-shadow:none;border:0;max-width:none}}}} @media(max-width:600px){{body{{padding:0}}main{{min-height:100vh;padding:30px 24px;border-radius:0}}}}
</style></head><body><main>{CONTENT_START}{content}{CONTENT_END}</main></body></html>"""


def _read(path: Path):
    raw = path.read_text(encoding="utf-8")
    title_match = re.search(r'<meta name="apex-notepad-title" content="([^"]*)"', raw)
    start, end = raw.find(CONTENT_START), raw.find(CONTENT_END)
    if start < 0 or end < start:
        raise ValueError("This is not an APEX Notepad document.")
    title = html.unescape(title_match.group(1)) if title_match else path.stem
    return title, raw[start + len(CONTENT_START):end]


def register_notepad_routes(app, require_user, config):
    def signed_in():
        user = require_user()
        if not user:
            return None, (jsonify({"error": "Sign in to use Notepad."}), 401)
        uid = user["id"] if hasattr(user, "keys") and "id" in user.keys() else getattr(user, "id", None)
        if not uid and isinstance(user, dict):
            uid = user.get("id")
        return str(uid), None

    @app.get("/api/notepad/documents")
    def list_notepad_documents():
        uid, error = signed_in()
        if error:
            return error
        root = _documents_root(config, uid)
        docs = []
        for path in root.glob("*.html"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                docs.append({"name": path.name, "modified_at": info.st_mtime, "size_bytes": info.st_size})
            except OSError:
                continue
        docs.sort(key=lambda item: item["modified_at"], reverse=True)
        return jsonify({"documents": docs, "directory": str(root)})

    @app.get("/api/notepad/documents/<name>")
    def get_notepad_document(name):
        uid, error = signed_in()
        if error:
            return error
        try:
            path = _path_for(_documents_root(config, uid), name)
            if not path.is_file() or path.is_symlink():
                return jsonify({"error": "Document not found."}), 404
            title, content = _read(path)
            return jsonify({"name": path.name, "title": title, "content": _sanitize(content), "modified_at": path.stat().st_mtime})
        except (OSError, UnicodeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/notepad/documents")
    def save_notepad_document():
        uid, error = signed_in()
        if error:
            return error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a document object."}), 400
        title = _safe_part(payload.get("title"), "Untitled")
        fragment = str(payload.get("content") or "")
        if len(fragment.encode("utf-8")) > MAX_CONTENT_BYTES:
            return jsonify({"error": "Document is too large (2 MB maximum)."}), 413
        try:
            root = _documents_root(config, uid)
            name = _document_name(payload.get("name"), title)
            path = _path_for(root, name)
            # New documents must not overwrite an existing title.
            if not payload.get("name"):
                stem = path.stem[:100]
                suffix = 2
                while path.exists():
                    name = f"{stem} ({suffix}).html"
                    path = _path_for(root, name)
                    suffix += 1
            document = _standalone(title, _sanitize(fragment))
            descriptor, temp_name = tempfile.mkstemp(prefix=".apex-notepad-", dir=root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            modified = path.stat().st_mtime
            return jsonify({
                "ok": True, "name": name, "title": title, "modified_at": modified,
                "download_url": f"/api/notepad/documents/{quote(name)}/download",
                "directory": str(root),
            })
        except (OSError, UnicodeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/notepad/documents/<name>/download")
    def download_notepad_document(name):
        uid, error = signed_in()
        if error:
            return error
        try:
            path = _path_for(_documents_root(config, uid), name)
            if not path.is_file() or path.is_symlink():
                return jsonify({"error": "Document not found."}), 404
            return send_file(path, as_attachment=True, download_name=path.name, mimetype="text/html")
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
