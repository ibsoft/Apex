"""Preview renderer for the window manager.

Converts user-bound backend file links into something the frontend can display
without any client-side document library:

  - .docx  -> a self-contained HTML document (python-docx)
  - .xlsx  -> a self-contained HTML document (openpyxl)
  - .pptx  -> a self-contained HTML document (python-pptx)
  - text-ish files / shell outputs -> a self-contained <pre> HTML document
  - images / pdfs  -> served inline through the renderer so <img>/<iframe>
                      work even for links whose download route is attachment-only
  - anything else   -> 415 (the window shows a "download" card instead)

Only the four known, signed/validated source URL shapes are accepted; every
other URL is rejected. The same signer salts, ownership checks and path
containment rules as the source download routes are reused.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from flask import Response, jsonify, request, send_file

DOCX_EXTENSIONS = {".docx", ".docm"}
XLSX_EXTENSIONS = {".xlsx", ".xlsm"}
PPTX_EXTENSIONS = {".pptx", ".pptm"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
    ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".sql",
    ".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm", ".xml",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}

_page_style = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#eceff3;color:#1c2733;
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:26px 34px 60px}
h1,h2,h3,h4,h5,h6{font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace;
letter-spacing:.06em;text-transform:uppercase;color:#10313d;margin:26px 0 12px}
h1{font-size:20px}h2{font-size:16px}h3{font-size:14px}
p{margin:0 0 12px}
ul,ol{margin:0 0 12px;padding-left:24px}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px}
th,td{border:1px solid #b9c3cf;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#dde4ec;color:#10313d;font-family:ui-monospace,Menlo,Consolas,monospace;
font-size:11px;letter-spacing:.05em;text-transform:uppercase}
img{max-width:100%;height:auto}
pre{background:#0b1722;color:#d9e7f2;padding:18px;border-radius:10px;
overflow:auto;font:12px/1.6 ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.meta{color:#5b6b7a;font-size:12px;margin:6px 0 24px;font-family:ui-monospace,Menlo,Consolas,monospace;letter-spacing:.03em}
hr{border:none;border-top:1px solid #c6d0da;margin:20px 0}
"""


class _PreviewError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_page_style}</style></head>"
        f"<body><div class='wrap'><div class='meta'>{html.escape(title)}</div>{body}</div></body></html>"
    )


def _html_response(page: str):
    return Response(page, mimetype="text/html; charset=utf-8")


def _send_inline(path: Path, name: str):
    mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = send_file(path, mimetype=mimetype, conditional=False, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _resolve_source(url: str, user_id: str, config):
    """Resolve one of the four known signed URL shapes to a local file.

    Returns (path, display_name). Raises _PreviewError for anything invalid.
    """
    parsed = urlsplit(url)
    path = parsed.path or ""
    if parsed.scheme not in ("", "http", "https") or parsed.netloc.startswith("@"):
        raise _PreviewError("Unsupported link.", 400)

    # ---- EDITOR documents (.docx / .xlsx) ----
    m = re.fullmatch(r"/api/editor/download/([A-Za-z0-9_.\-]+)", path)
    if m:
        from tools.editor_tools import _signer as editor_signer, _fingerprint

        ttl = getattr(config, "EDITOR_FILE_TTL_SECONDS", 3600)
        try:
            ticket = editor_signer(config).loads(m.group(1), max_age=ttl)
        except Exception as exc:
            raise _PreviewError(_ticket_message("link", exc), _ticket_status(exc))
        if not isinstance(ticket, dict) or ticket.get("user") != user_id:
            raise _PreviewError("This link belongs to a different user.", 403)
        try:
            target = Path(ticket["path"]).resolve()
            base_dir = (
                Path(getattr(config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
                / "generated" / "editor"
            ).resolve()
            if not target.is_relative_to(base_dir) or not target.is_file():
                raise _PreviewError("File is unavailable.", 404)
            if _fingerprint(target.stat()) != ticket.get("fingerprint"):
                raise _PreviewError("File changed. Generate it again.", 409)
        except _PreviewError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise _PreviewError("File is unavailable.", 404)
        return target, (ticket.get("filename") or target.name)

    # ---- run_shell on-screen output (text) ----
    m = re.fullmatch(r"/api/shell/download/([A-Za-z0-9_.\-]+)", path)
    if m:
        from tools.shell_out import _fingerprint as shell_fingerprint, signer as shell_signer

        ttl = getattr(config, "SHELL_OUT_TTL_SECONDS", 3600)
        try:
            ticket = shell_signer(config).loads(m.group(1), max_age=ttl)
        except Exception as exc:
            raise _PreviewError(_ticket_message("link", exc), _ticket_status(exc))
        if not isinstance(ticket, dict) or ticket.get("user") != user_id:
            raise _PreviewError("This link belongs to a different user.", 403)
        try:
            target = Path(ticket["path"]).resolve()
            base_dir = (
                Path(getattr(config, "DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
                / "generated" / "shell"
            ).resolve()
            if not target.is_relative_to(base_dir) or not target.is_file():
                raise _PreviewError("File is unavailable.", 404)
            if shell_fingerprint(target.stat()) != ticket.get("fingerprint"):
                raise _PreviewError("File changed. Run the command again.", 409)
        except _PreviewError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise _PreviewError("File is unavailable.", 404)
        return target, (ticket.get("filename") or target.name)

    # ---- file-search results ----
    m = re.fullmatch(r"/api/files/download/([A-Za-z0-9_.\-]+)", path)
    if m:
        from tools.file_search import fingerprint as fs_fingerprint, permitted, signer

        try:
            ticket = signer(config).loads(m.group(1), max_age=3600)
        except Exception as exc:
            raise _PreviewError(_ticket_message("link", exc), _ticket_status(exc))
        if not isinstance(ticket, dict) or ticket.get("user") != user_id:
            raise _PreviewError("This link belongs to a different user.", 403)
        try:
            target = Path(ticket["path"])
            if not permitted(target, config):
                raise _PreviewError("This file is outside the permitted search roots.", 403)
            info = target.stat()
            if not target.is_file():
                raise _PreviewError("File is unavailable.", 404)
            if fs_fingerprint(info) != ticket.get("fingerprint"):
                raise _PreviewError("File changed. Search again.", 409)
        except _PreviewError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise _PreviewError("File is unavailable.", 404)
        return target, target.name

    # ---- image browser ----
    m = re.fullmatch(r"/api/images/file/([A-Za-z0-9_.\-]+)", path)
    if m:
        from tools.image_browser import _fingerprint as img_fingerprint
        from tools.image_browser import _images_root as images_root
        from tools.image_browser import _signer as image_signer
        from tools.image_browser import LINK_SECONDS as IMAGE_LINK_SECONDS

        try:
            ticket = image_signer(config).loads(m.group(1), max_age=IMAGE_LINK_SECONDS)
        except Exception as exc:
            raise _PreviewError(_ticket_message("link", exc), _ticket_status(exc))
        if not isinstance(ticket, dict) or ticket.get("user") not in (None, user_id):
            raise _PreviewError("This link belongs to a different user.", 403)
        try:
            target = Path(ticket["path"]).resolve()
            root = images_root(config)
            if root is None or not target.is_relative_to(root) or not target.is_file():
                raise _PreviewError("Image is unavailable.", 404)
            if img_fingerprint(target.stat()) != ticket.get("fingerprint"):
                raise _PreviewError("Image changed. Search again.", 409)
        except _PreviewError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise _PreviewError("Image is unavailable.", 404)
        return target, target.name

    # ---- Obsidian attachments (path-based, vault-contained) ----
    if parsed.path == "/api/obsidian/file":
        query = dict(parse_qsl(parsed.query))
        rel = (query.get("path") or "").strip()
        if not rel:
            raise _PreviewError("Missing path.", 400)
        from tools.obsidian_tools import _is_md, _resolve, _vault_or_error

        vault, err = _vault_or_error(config)
        if err:
            raise _PreviewError(err, 400)
        target, err = _resolve(vault, rel)
        if err:
            raise _PreviewError(err, 400)
        if not target.exists() or _is_md(target):
            raise _PreviewError("Attachment not found or is a note.", 404)
        return target, target.name

    raise _PreviewError("Unsupported link.", 400)


def _ticket_message(label: str, exc: Exception) -> str:
    from itsdangerous import SignatureExpired

    if isinstance(exc, SignatureExpired):
        return f"Preview {label} expired. Generate it again."
    return f"Invalid preview {label}."


def _ticket_status(exc: Exception) -> int:
    from itsdangerous import SignatureExpired

    return 410 if isinstance(exc, SignatureExpired) else 404


def _kind_for(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in DOCX_EXTENSIONS:
        return "docx"
    if ext in XLSX_EXTENSIONS:
        return "xlsx"
    if ext in PPTX_EXTENSIONS:
        return "pptx"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return None


# ---- docx -> HTML ----------------------------------------------------------

def _iter_blocks(document):
    """Body-level paragraphs and tables in document order (version-agnostic)."""
    from docx.oxml.ns import qn
    from docx.table import Table as _Table
    from docx.text.paragraph import Paragraph as _Paragraph

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield _Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield _Table(child, document)


def _docx_html(path: Path, name: str) -> str:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    rels = document.part.rels
    body_parts: list[str] = []

    def image_tags(element) -> str:
        tags: list[str] = []
        for blip in element.iter(qn("a:blip")):
            r_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            if not r_id or r_id not in rels:
                continue
            try:
                part = rels[r_id].target_part
                content_type = getattr(part, "content_type", "") or ""
                if content_type.startswith("image/"):
                    blob = base64.b64encode(part.blob).decode("ascii")
                    tags.append(f"<img src='data:{content_type};base64,{blob}' alt=''/>")
            except Exception:
                continue
        return "\n".join(tags)

    def paragraph_html(paragraph: Paragraph) -> str:
        segments: list[str] = []
        for child in paragraph._p.iterchildren():
            if child.tag == qn("w:r"):
                text = "".join(t.text or "" for t in child.findall(qn("w:t")))
                if text:
                    segments.append(_escape(text))
                segments.append(image_tags(child))
            elif child.tag in (qn("w:drawing"), qn("w:pict")):
                segments.append(image_tags(child))
        inner = "".join(segments) or "&nbsp;"
        style = (paragraph.style.name or "").lower()
        if style.startswith("title"):
            return f"<h1>{inner}</h1>"
        if style == "subtitle":
            return f"<h2>{inner}</h2>"
        if style.startswith("heading"):
            try:
                level = int(style.split()[-1])
            except (ValueError, IndexError):
                level = 1
            return f"<h{min(max(level, 1), 6)}>{inner}</h{min(max(level, 1), 6)}>"
        return f"<p>{inner}</p>"

    def table_html(table: Table) -> str:
        rows_html = []
        for row_index, row in enumerate(table.rows):
            cells = []
            for cell in row.cells:
                tag = "th" if row_index == 0 else "td"
                cells.append(f"<{tag}>{_escape(cell.text)}</{tag}>")
            rows_html.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows_html)}</table>"

    list_open: str | None = None
    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            style = (block.style.name or "").lower()
            tag = None
            if style.startswith("list bullet"):
                tag = "ul"
            elif style.startswith("list number"):
                tag = "ol"
            if tag:
                if list_open != tag:
                    if list_open:
                        body_parts.append(f"</{list_open}>")
                    body_parts.append(f"<{tag}>")
                    list_open = tag
                body_parts.append(f"<li>{_p_inner(block, rels)}</li>")
            else:
                if list_open:
                    body_parts.append(f"</{list_open}>")
                    list_open = None
                body_parts.append(paragraph_html(block))
        elif isinstance(block, Table):
            if list_open:
                body_parts.append(f"</{list_open}>")
                list_open = None
            body_parts.append(table_html(block))

    if list_open:
        body_parts.append(f"</{list_open}>")

    inner = "\n".join(body_parts)
    if not inner.strip() or inner.strip() == "&nbsp;":
        inner = "<p>This document has no visible text.</p>"
    return _page(name, inner)


def _p_inner(paragraph: Paragraph, rels) -> str:
    from docx.oxml.ns import qn

    segments: list[str] = []
    for child in paragraph._p.iterchildren():
        if child.tag == qn("w:r"):
            text = "".join(t.text or "" for t in child.findall(qn("w:t")))
            if text:
                segments.append(_escape(text))
            for blip in child.iter(qn("a:blip")):
                r_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
                if r_id and r_id in rels:
                    try:
                        part = rels[r_id].target_part
                        if part.content_type.startswith("image/"):
                            blob = base64.b64encode(part.blob).decode("ascii")
                            segments.append(f"<img src='data:{part.content_type};base64,{blob}' alt=''/>")
                    except Exception:
                        continue
    return "".join(segments) or "&nbsp;"


# ---- xlsx -> HTML ----------------------------------------------------------

def _xlsx_html(path: Path, name: str) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=False)
    sections: list[str] = []
    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows():
            values = [cell.value for cell in row]
            if not any(value is not None and str(value).strip() for value in values):
                continue
            rows.append(["" if value is None else value for value in values])
        if not rows:
            continue
        header = rows[0]
        body_rows = rows[1:]
        header_cells = "".join(f"<th>{_escape(cell)}</th>" for cell in header)
        row_html = []
        for row in body_rows:
            row_html.append(
                "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>"
            )
        sections.append(
            f"<h2>{_escape(sheet.title)}</h2>"
            f"<table><thead><tr>{header_cells}</tr></thead>"
            f"<tbody>{''.join(row_html)}</tbody></table>"
        )
    if not sections:
        sections.append("<p>This workbook has no visible data.</p>")
    return _page(name, "\n".join(sections))


# ---- pptx -> HTML ----------------------------------------------------------

_PPTX_IMAGE_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp", "svg": "image/svg+xml",
}


def _pptx_html(path: Path, name: str) -> str:
    try:
        from pptx import Presentation  # type: ignore
    except Exception as exc:
        raise _PreviewError(f"This server cannot preview PowerPoint files: {exc}", 415)

    try:
        presentation = Presentation(str(path))
    except Exception:
        raise _PreviewError("This PowerPoint file could not be read.", 400)

    def frame_html(shape) -> str:
        if not shape.has_text_frame:
            return ""
        parts: list[str] = []
        for para in shape.text_frame.paragraphs:
            runs = "".join(_escape(run.text) for run in para.runs) or _escape(para.text)
            if not runs.strip():
                continue
            if para.level and para.level > 0:
                parts.append(f"<p style='padding-left:{min(para.level, 5) * 20}px'>• {runs}</p>")
            else:
                parts.append(f"<p>{runs}</p>")
        return "".join(parts)

    def table_html(shape) -> str:
        if not shape.has_table:
            return ""
        rows_html = []
        for row_index, row in enumerate(shape.table.rows):
            cells = []
            for cell in row.cells:
                tag = "th" if row_index == 0 else "td"
                cells.append(f"<{tag}>{_escape(cell.text)}</{tag}>")
            rows_html.append(f"<tr>{''.join(cells)}</tr>")
        return f"<table>{''.join(rows_html)}</table>"

    def picture_html(shape) -> str:
        try:
            image = getattr(shape, "image", None)
            if image is None:
                return ""
            blob = image.blob
            ctype = image.content_type or ""
            if ctype.startswith("image/"):
                return f"<p><img src='data:{ctype};base64,{base64.b64encode(blob).decode('ascii')}' alt=''/></p>"
        except Exception:
            return ""
        return ""

    def shape_html(shape) -> str:
        chunks = []
        if hasattr(shape, "shapes"):
            for child in shape.shapes:
                chunks.append(shape_html(child))
            return "".join(chunks)
        chunks.append(frame_html(shape))
        chunks.append(table_html(shape))
        chunks.append(picture_html(shape))
        return "".join(chunks)

    slides_html = []
    for index, slide in enumerate(presentation.slides, start=1):
        if getattr(slide, "slide_layout", None) is not None and getattr(slide.slide_layout, "name", None):
            tag = _escape(slide.slide_layout.name) or ""
        else:
            tag = ""
        body = "".join(shape_html(shape) for shape in slide.shapes)
        if tag:
            tag = f"<span class='meta' style='float:right'>{tag}</span>"
        slides_html.append(
            f"<h2>Slide {index}{(' — ' + tag) if tag else ''}</h2>" + (body or "<p><i>(empty slide)</i></p>")
        )

    inner = "\n".join(slides_html)
    return _page(name, inner)


# ---- text -> HTML ----------------------------------------------------------

_TEXT_MODE_LIMIT = 2 * 1024 * 1024


def _text_html(path: Path, name: str) -> str:
    try:
        raw = path.read_bytes()[: _TEXT_MODE_LIMIT]
    except OSError:
        raise _PreviewError("File is unreadable.", 404)
    decoder = "utf-8" if path.suffix.lower() not in (".csv", ".tsv") else "utf-8-sig"
    try:
        text = raw.decode(decoder, errors="replace")
    except Exception:
        text = raw.decode("latin-1", errors="replace")
    if raw and len(raw) == _TEXT_MODE_LIMIT:
        text += "\n… (preview truncated)"
    return _page(name, f"<pre>{_escape(text)}</pre>")


# ---- route -----------------------------------------------------------------

def register_preview_routes(app, require_user, config):
    @app.get("/api/preview/render")
    def preview_render():
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to preview files."}), 401
        url = (request.args.get("url") or "").strip()
        if not url or len(url) > 2048:
            return jsonify({"error": "Missing or invalid preview url."}), 400
        try:
            target, name = _resolve_source(url, str(user["id"]), config)
        except _PreviewError as exc:
            return jsonify({"error": exc.message}), exc.status
        except Exception:
            return jsonify({"error": "Could not resolve the preview."}), 404

        kind = _kind_for(name)
        if kind == "docx":
            return _html_response(_docx_html(target, name))
        if kind == "xlsx":
            return _html_response(_xlsx_html(target, name))
        if kind == "pptx":
            return _html_response(_pptx_html(target, name))
        if kind == "text":
            return _html_response(_text_html(target, name))
        if kind in ("image", "pdf"):
            return _send_inline(target, name)
        return jsonify({"error": "This file type cannot be previewed."}), 415

    @app.get("/api/preview/kind")
    def preview_kind():
        """Classify a signed preview token's real file kind.

        Signed editor/files/obsidian tokens hide the filename and extension
        server-side (no extension in the URL), so the frontend cannot guess
        whether a Word/Excel/PDF/text file should render as a preview or a
        download card. This endpoint resolves the token with the same
        ownership/URL/source checks as the render route and returns just the
        kind (no content), letting the frontend pick the right window body.
        """
        user = require_user()
        if user is None:
            return jsonify({"error": "Sign in to preview files."}), 401
        url = (request.args.get("url") or "").strip()
        if not url or len(url) > 2048:
            return jsonify({"error": "Missing or invalid preview url."}), 400
        try:
            _, name = _resolve_source(url, str(user["id"]), config)
        except _PreviewError as exc:
            return jsonify({"error": exc.message}), exc.status
        except Exception:
            return jsonify({"error": "Could not resolve the preview."}), 404
        kind = _kind_for(name)
        return jsonify({"kind": kind or "other"})