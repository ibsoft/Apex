"""Agent bridge for the live Notepad editor and its saved documents."""
import json

from tools.base import Tool
from tools.notepad import _documents_root, _path_for, _read, _sanitize

ACTIONS = ["open", "close", "focus", "minimize", "maximize", "restore", "new", "save", "download", "export_text", "write", "replace", "clear", "title", "recent", "hide_recent", "open_document", "undo", "redo", "select_all", "format", "read", "command_output", "list_documents", "read_document"]


def build_notepad_tools(config):
    def control(args, ctx):
        if not ctx.user_id:
            return "Sign in to control Notepad."
        action = args.get("action")
        content = args.get("content", "")
        if action not in ACTIONS or not isinstance(content, str):
            return "Invalid Notepad action or content."
        if len(content.encode("utf-8")) > 2 * 1024 * 1024:
            return "Notepad content exceeds the 2 MB limit."
        try:
            if action == "list_documents":
                root = _documents_root(config, ctx.user_id)
                return json.dumps({"documents": sorted(p.name for p in root.glob("*.html") if p.is_file() and not p.is_symlink())})
            if action == "read_document":
                path = _path_for(_documents_root(config, ctx.user_id), content)
                title, fragment = _read(path)
                return json.dumps({"title": title, "content": _sanitize(fragment)}, ensure_ascii=False)
            return json.dumps({"notepad_command": {"type": "notepad", "action": action, "content": content}, "status": "Requested browser action; the editor will report success or failure. Do not claim completion before its acknowledgement."}, ensure_ascii=False)
        except (OSError, ValueError, UnicodeError) as exc:
            return f"Notepad error: {exc}"

    return [Tool("notepad_control", "Control the user's live Notepad via voice or text. Targets the focused Notepad, or the latest on the current desktop; opens one if needed. write APPENDS exact plain text; replace replaces the entire document; title renames its title; open_document opens a saved filename; list_documents/read_document inspect saved documents. read asks the browser to show current text; use Live Notepad documents in the prompt to reason about unsaved text. format applies to all text: bold, italic, underline, strikethrough, heading 1, heading 2, paragraph, bullet list, numbered list, align left, align center, align right, remove formatting. For compound tasks, call tools in order. To add command output, run the requested command ONCE using a terminal tool, then pass its actual output to write. command_output copies the latest available command result; never insert the literal words 'command output' or invent output. Do not execute shell commands merely to write their text. For generated content, generate the text then pass it to write. Browser actions are requests, not confirmed completion.", {
        "type": "object", "properties": {"action": {"type": "string", "enum": ACTIONS}, "content": {"type": "string", "description": "Exact text, saved filename, title, or format. Empty for actions without content."}}, "required": ["action", "content"], "additionalProperties": False,
    }, control)]
