"""Obsidian tools: read, write, search and navigate a local Obsidian vault."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

from tools.base import Tool, ToolContext


def _vault_or_error(config):
    path = getattr(config, "OBSIDIAN_VAULT_PATH", None)
    if not path:
        return None, "Obsidian vault is not configured. Set OBSIDIAN_VAULT_PATH to your vault folder."
    vault = Path(path).expanduser().resolve()
    if not vault.exists():
        return None, f"Obsidian vault path does not exist: {vault}"
    if not vault.is_dir():
        return None, f"Obsidian vault path is not a directory: {vault}"
    return vault, None


def _resolve(vault: Path, rel: str) -> tuple[Path, str | None]:
    rel = rel.strip().lstrip("/")
    target = (vault / rel).resolve()
    try:
        target.relative_to(vault)
    except ValueError:
        return target, "Path escapes the Obsidian vault."
    return target, None


def _is_md(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".md", ".markdown"}


def _all_files(vault: Path, subpath: str = "", ext_filter: set[str] | None = None):
    base, err = _resolve(vault, subpath)
    if err:
        return []
    start = base if base.is_dir() else base.parent
    files = []
    for root, _, names in os.walk(start):
        for name in names:
            p = Path(root) / name
            if ext_filter is None:
                files.append(p)
            elif p.suffix.lower() in ext_filter:
                files.append(p)
            elif ext_filter and "" in ext_filter and not p.suffix:
                files.append(p)
    files.sort()
    return files


def _all_notes(vault: Path, subpath: str = ""):
    return _all_files(vault, subpath, {".md", ".markdown"})


def _frontmatter(path: Path) -> tuple[dict, str]:
    """Return (frontmatter dict, remaining body)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}, ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, text[m.end():]


def _note_title(vault: Path, path: Path) -> str:
    rel = path.relative_to(vault)
    fm, _ = _frontmatter(path)
    if isinstance(fm, dict) and fm.get("title"):
        return str(fm["title"])
    return rel.with_suffix("").as_posix()


def _wikilink_re(title: str) -> re.Pattern:
    # Match [[Title]] or [[Title|alias]] even if alias contains brackets? no.
    return re.compile(r"\[\[" + re.escape(title) + r"(?:\|[^\]]+)?\]\]")


def build_obsidian_tools(config) -> list[Tool]:
    def t_obsidian_list_notes(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        subpath = (args.get("path") or "").strip()
        limit = min(int(args.get("limit", 50) or 50), 200)
        files = _all_notes(vault, subpath)[:limit]
        rels = [str(f.relative_to(vault)) for f in files]
        return "\n".join(rels) if rels else "No Markdown notes found."

    def t_obsidian_read_note(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not target.exists():
            return f"Note not found: {rel}"
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        max_chars = min(int(args.get("max_chars", 12000) or 12000), 50000)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Could not read note: {exc}"
        return text[:max_chars]

    def t_obsidian_create_note(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        content = args.get("content") or ""
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if target.exists() and not args.get("overwrite"):
            return f"Note already exists: {rel}. Set overwrite=true to replace it."
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            return f"Could not create note: {exc}"
        return f"Created {rel}"

    def t_obsidian_update_note(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        content = args.get("content") if args.get("content") is not None else None
        if not rel or content is None:
            return "Please provide the note path and content."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not target.exists():
            return f"Note not found: {rel}. Use create_note to create it."
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            return f"Could not update note: {exc}"
        return f"Updated {rel}"

    def t_obsidian_delete_note(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not target.exists():
            return f"Note not found: {rel}"
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        try:
            target.unlink()
        except Exception as exc:
            return f"Could not delete note: {exc}"
        return f"Deleted {rel}"

    def t_obsidian_create_folder(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the folder path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return f"Could not create folder: {exc}"
        return f"Created folder {rel}"

    def t_obsidian_delete_folder(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        recursive = bool(args.get("recursive", False))
        if not rel:
            return "Please provide the folder path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not target.exists():
            return f"Folder not found: {rel}"
        if not target.is_dir():
            return f"Not a folder: {rel}"
        try:
            if recursive:
                import shutil
                shutil.rmtree(target)
            else:
                target.rmdir()
        except Exception as exc:
            return f"Could not delete folder: {exc}"
        return f"Deleted folder {rel}"

    def t_obsidian_search_notes(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        query = (args.get("query") or "").strip().lower()
        if not query:
            return "Please provide a search query."
        limit = min(int(args.get("limit", 20) or 20), 100)
        files = _all_notes(vault)
        matches = []
        for f in files:
            rel = str(f.relative_to(vault))
            if query in rel.lower():
                matches.append((rel, ""))
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if query in text.lower():
                idx = text.lower().find(query)
                snippet = text[max(0, idx - 120):idx + 200].replace("\n", " ")
                matches.append((rel, snippet))
            if len(matches) >= limit:
                break
        if not matches:
            return f"No notes matched `{query}`."
        lines = []
        for rel, snippet in matches:
            lines.append(f"- {rel}")
            if snippet:
                lines.append(f"  …{snippet}…")
        return "\n".join(lines)

    def t_obsidian_get_outgoing_links(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        text = target.read_text(encoding="utf-8", errors="replace")
        links = []
        for m in re.finditer(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", text):
            links.append({"target": m.group(1).strip(), "alias": (m.group(2) or "").strip()})
        return f"Outgoing links from {rel}:\n" + "\n".join(f"- {l['target']}" + (f" (alias: {l['alias']})" if l["alias"] else "") for l in links) if links else f"No outgoing links in {rel}."

    def t_obsidian_get_backlinks(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        title = _note_title(vault, target)
        name_variants = {title, Path(rel).with_suffix("").as_posix(), target.stem}
        files = _all_notes(vault)
        backlinks = []
        for f in files:
            if f == target:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for variant in name_variants:
                if _wikilink_re(variant).search(text):
                    backlinks.append(str(f.relative_to(vault)))
                    break
        return "\n".join(backlinks) if backlinks else f"No backlinks found for {rel}."

    def t_obsidian_follow_link(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        link = (args.get("link") or "").strip()
        from_path = (args.get("from_path") or "").strip()
        if not link:
            return "Please provide the link to follow."
        # Strip alias if present
        link = link.split("|")[0].strip()
        candidates = []
        # Direct filename match
        direct, derr = _resolve(vault, link if link.endswith((".md", ".markdown")) else link + ".md")
        if not derr and direct.exists():
            candidates.append(direct)
        # Search by stem
        for f in _all_notes(vault):
            if f.stem.lower() == link.lower() or f.name.lower() == link.lower() or (link.lower() + ".md") == f.name.lower():
                if f not in candidates:
                    candidates.append(f)
        if from_path:
            from_file, ferr = _resolve(vault, from_path)
            if not ferr and from_file.exists():
                sibling = (from_file.parent / (link + ".md")).resolve()
                try:
                    sibling.relative_to(vault)
                    if sibling.exists() and sibling not in candidates:
                        candidates.insert(0, sibling)
                except ValueError:
                    pass
        if not candidates:
            return f"Could not resolve link `[[{link}]]` to a note."
        chosen = candidates[0]
        max_chars = min(int(args.get("max_chars", 12000) or 12000), 50000)
        text = chosen.read_text(encoding="utf-8", errors="replace")
        return f"Note: {chosen.relative_to(vault)}\n\n{text[:max_chars]}"

    def t_obsidian_get_note_metadata(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the note path."
        target, err = _resolve(vault, rel)
        if err:
            return err
        if not _is_md(target):
            return f"Not a Markdown note: {rel}"
        fm, body = _frontmatter(target)
        if not isinstance(fm, dict):
            fm = {}
        inline_tags = sorted(set(re.findall(r"#([\w\-]+)", body)))
        data = {
            "path": rel,
            "title": _note_title(vault, target),
            "frontmatter": fm,
            "inline_tags": inline_tags,
            "word_count": len(body.split()),
        }
        return f"```json\n{yaml.safe_dump(data, allow_unicode=True)}\n```"

    def t_obsidian_search_by_tag(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        tag = (args.get("tag") or "").strip().lstrip("#")
        if not tag:
            return "Please provide a tag."
        limit = min(int(args.get("limit", 20) or 20), 100)
        files = _all_notes(vault)
        matches = []
        for f in files:
            fm, body = _frontmatter(f)
            fm_tags = []
            if isinstance(fm, dict):
                t = fm.get("tags")
                if isinstance(t, list):
                    fm_tags = [str(x).lstrip("#") for x in t]
                elif isinstance(t, str):
                    fm_tags = [x.lstrip("#") for x in t.split()]
            inline_tags = set(re.findall(r"#([\w\-]+)", body))
            all_tags = set(fm_tags) | inline_tags
            if tag.lower() in {t.lower() for t in all_tags}:
                matches.append(str(f.relative_to(vault)))
            if len(matches) >= limit:
                break
        return "\n".join(matches) if matches else f"No notes found with tag `#{tag}`."

    def t_obsidian_daily_note(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        folder = getattr(config, "OBSIDIAN_DAILY_NOTES_FOLDER", "") or ""
        fmt = getattr(config, "OBSIDIAN_DAILY_NOTES_FORMAT", "%Y-%m-%d") or "%Y-%m-%d"
        filename = datetime.now().strftime(fmt) + ".md"
        rel = f"{folder}/{filename}".lstrip("/") if folder else filename
        target, derr = _resolve(vault, rel)
        if derr:
            return derr
        created = False
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            heading = datetime.now().strftime("%Y-%m-%d")
            target.write_text(f"# {heading}\n\n", encoding="utf-8")
            created = True
        text = target.read_text(encoding="utf-8", errors="replace")
        return f"{'Created' if created else 'Opened'} {rel}\n\n{text}"

    def t_obsidian_list_attachments(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        subpath = (args.get("path") or "").strip()
        limit = min(int(args.get("limit", 50) or 50), 200)
        files = [f for f in _all_files(vault, subpath) if not _is_md(f)][:limit]
        rels = [str(f.relative_to(vault)) for f in files]
        return "\n".join(rels) if rels else "No attachments found."

    def t_obsidian_attachment_url(args, ctx: ToolContext):
        vault, err = _vault_or_error(config)
        if err:
            return err
        rel = (args.get("path") or "").strip()
        if not rel:
            return "Please provide the attachment path."
        target, aerr = _resolve(vault, rel)
        if aerr:
            return aerr
        if not target.exists() or _is_md(target):
            return f"Attachment not found or is a note: {rel}"
        base_url = getattr(config, "BASE_URL", "http://localhost:5001").rstrip("/")
        return f"{base_url}/api/obsidian/file?path={quote(rel, safe='')}" if rel else err

    schemas = {
        "obsidian_list_notes": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional subfolder path inside the vault."},
                "limit": {"type": "integer", "default": 50},
            },
        },
        "obsidian_read_note": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the Markdown note."},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["path"],
        },
        "obsidian_create_note": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path for the new note."},
                "content": {"type": "string", "description": "Markdown content."},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        },
        "obsidian_update_note": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the note."},
                "content": {"type": "string", "description": "New Markdown content."},
            },
            "required": ["path", "content"],
        },
        "obsidian_delete_note": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative path to the note."}},
            "required": ["path"],
        },
        "obsidian_create_folder": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative folder path."}},
            "required": ["path"],
        },
        "obsidian_delete_folder": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative folder path."},
                "recursive": {"type": "boolean", "default": False, "description": "Delete folder and all contents."},
            },
            "required": ["path"],
        },
        "obsidian_search_notes": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
        "obsidian_get_outgoing_links": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative note path."}},
            "required": ["path"],
        },
        "obsidian_get_backlinks": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative note path."}},
            "required": ["path"],
        },
        "obsidian_follow_link": {
            "type": "object",
            "properties": {
                "link": {"type": "string", "description": "Wiki-link target, e.g. 'Black horse' or 'Black horse|alias'."},
                "from_path": {"type": "string", "description": "Optional note path the link is in, for relative resolution."},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["link"],
        },
        "obsidian_get_note_metadata": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative note path."}},
            "required": ["path"],
        },
        "obsidian_search_by_tag": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Tag without #."},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["tag"],
        },
        "obsidian_daily_note": {"type": "object", "properties": {}},
        "obsidian_list_attachments": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional subfolder path."},
                "limit": {"type": "integer", "default": 50},
            },
        },
        "obsidian_attachment_url": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Vault-relative attachment path."}},
            "required": ["path"],
        },
    }

    return [
        Tool("obsidian_list_notes", "List Markdown notes in the vault.", schemas["obsidian_list_notes"], t_obsidian_list_notes),
        Tool("obsidian_read_note", "Read one Markdown note by vault-relative path.", schemas["obsidian_read_note"], t_obsidian_read_note),
        Tool("obsidian_create_note", "Create a new Markdown note (creates parent folders automatically).", schemas["obsidian_create_note"], t_obsidian_create_note),
        Tool("obsidian_update_note", "Overwrite an existing Markdown note.", schemas["obsidian_update_note"], t_obsidian_update_note),
        Tool("obsidian_delete_note", "Delete a Markdown note.", schemas["obsidian_delete_note"], t_obsidian_delete_note),
        Tool("obsidian_create_folder", "Create a folder inside the vault.", schemas["obsidian_create_folder"], t_obsidian_create_folder),
        Tool("obsidian_delete_folder", "Delete a folder. Use recursive=true to delete non-empty folders.", schemas["obsidian_delete_folder"], t_obsidian_delete_folder),
        Tool("obsidian_search_notes", "Full-text search across note filenames and contents.", schemas["obsidian_search_notes"], t_obsidian_search_notes),
        Tool("obsidian_get_outgoing_links", "List all [[wiki-links]] inside a note.", schemas["obsidian_get_outgoing_links"], t_obsidian_get_outgoing_links),
        Tool("obsidian_get_backlinks", "List notes that link to the given note.", schemas["obsidian_get_backlinks"], t_obsidian_get_backlinks),
        Tool("obsidian_follow_link", "Resolve a [[wiki-link]] to its target note and return its content.", schemas["obsidian_follow_link"], t_obsidian_follow_link),
        Tool("obsidian_get_note_metadata", "Read a note's YAML frontmatter, inline tags, title and word count.", schemas["obsidian_get_note_metadata"], t_obsidian_get_note_metadata),
        Tool("obsidian_search_by_tag", "Find notes by frontmatter or inline #tag.", schemas["obsidian_search_by_tag"], t_obsidian_search_by_tag),
        Tool("obsidian_daily_note", "Open today's daily note, creating it if it does not exist.", schemas["obsidian_daily_note"], t_obsidian_daily_note),
        Tool("obsidian_list_attachments", "List non-Markdown files (images, PDFs, etc.) in the vault.", schemas["obsidian_list_attachments"], t_obsidian_list_attachments),
        Tool("obsidian_attachment_url", "Return a backend URL that serves an attachment so it can be shown in chat.", schemas["obsidian_attachment_url"], t_obsidian_attachment_url),
    ]
