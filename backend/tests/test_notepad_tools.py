from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.base import ToolContext, ToolRegistry
from tools.notepad import _documents_root, _standalone
from tools.notepad_tools import build_notepad_tools
from agent.base import AgentContext


class NotepadToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = SimpleNamespace(NOTEPAD_DOCUMENTS_DIR=self.tmp.name)
        self.tool = build_notepad_tools(self.config)[0]
        self.ctx = ToolContext(user_id="alice")

    def test_edit_preserves_exact_output_without_executing_it(self):
        content = "uname -a\nHello, κόσμε!\n"
        result = json.loads(self.tool.call({"action": "write", "content": content}, self.ctx))
        self.assertEqual(result["notepad_command"], {"type": "notepad", "action": "write", "content": content})
        self.assertIn("Requested", result["status"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_invalid_or_anonymous_requests_do_not_issue_commands(self):
        for args in ({"action": "shell"}, {"action": "write", "content": []}, {"action": "write", "content": "x" * (2 * 1024 * 1024 + 1)}):
            self.assertNotIn("notepad_command", self.tool.call(args, self.ctx))
        self.assertIn("Sign in", self.tool.call({"action": "open"}, ToolContext()))

    def test_saved_documents_are_isolated_and_sanitized(self):
        root = _documents_root(self.config, "alice")
        (root / "Notes.html").write_text(_standalone("Notes", '<b>Hello</b><img src=x onerror=alert(1)>'))
        listed = json.loads(self.tool.call({"action": "list_documents"}, self.ctx))
        self.assertEqual(listed["documents"], ["Notes.html"])
        read = json.loads(self.tool.call({"action": "read_document", "content": "Notes.html"}, self.ctx))
        self.assertEqual(read["content"], "<b>Hello</b>")
        self.assertEqual(json.loads(self.tool.call({"action": "list_documents"}, ToolContext(user_id="bob")))["documents"], [])
        self.assertIn("error", self.tool.call({"action": "read_document", "content": "../alice/Notes.html"}, self.ctx))

    def test_notepad_available_with_specialist_skill_filter(self):
        registry = ToolRegistry().register(self.tool)
        ctx = AgentContext(user_id="alice", conversation_id="c", system_prompt="", history=[], provider=None, provider_kind="test", engine_name="test", tools=registry, skill_tools=["terminal_command"])
        self.assertEqual([tool.name for tool in ctx.active_tools()], ["notepad_control"])
