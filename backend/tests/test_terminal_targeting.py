"""A request aimed at one numbered terminal must run in that window.

"open top on terminal 4" reached the model as an ordinary request: it read
"open" as "start a new terminal" and ran `top` in a fresh window while the
operator watched another one. These tests pin the two halves of the fix — the
frontend parser that extracts the number, and the prompt note that stops the
model from substituting a new terminal for the named one.
"""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import terminal_target_note

COMMANDS_TS = ROOT / "frontend" / "lib" / "commands.ts"
FRONTEND_DIR = ROOT / "frontend"

# Transpile the real frontend parser and call it, the same way the frontend
# suite does. Running the actual source is the point: the number the backend is
# told about has to be the number the title bar shows, and the two drifting
# apart is the original bug.
_HARNESS = """
const fs = require('fs'), ts = require('typescript');
const out = ts.transpileModule(fs.readFileSync(process.argv[1], 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const m = {};
new Function('exports', out)(m);
process.stdout.write(JSON.stringify(m.parseTerminalTarget(process.argv[2], process.argv[3])));
"""


def parse_terminal_target(text: str, language: str = "en"):
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(COMMANDS_TS), text, language],
        cwd=FRONTEND_DIR, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"parser harness failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


class TerminalTargetNoteTests(unittest.TestCase):
    def test_note_is_only_added_for_a_named_window(self):
        self.assertEqual(terminal_target_note(0, "abc123def456"), "")
        self.assertEqual(terminal_target_note(-1, "abc123def456"), "")
        self.assertEqual(terminal_target_note(4, ""), "")

    def test_note_names_the_window_and_forbids_a_new_terminal(self):
        note = terminal_target_note(4, "abc123def456")
        self.assertIn("terminal #4", note)
        self.assertIn("THAT window", note)
        # The two things the model actually got wrong.
        self.assertIn("never open an additional terminal", note)
        self.assertIn("do not pass the `terminal` argument", note)


class TerminalTargetParsingTests(unittest.TestCase):
    def test_the_number_heard_is_the_number_delivered(self):
        for text, language, number in (
            ("open top on terminal 4", "en", 4),
            ("run top on terminal 2", "en", 2),
            ("execute ping 8.8.8.8 on terminal 1 please", "en", 1),
            ("open top on terminal #3", "en", 3),
            ("άνοιξε το top στο τερματικό 2", "el", 2),
        ):
            with self.subTest(text=text):
                parsed = parse_terminal_target(text, language)
                self.assertIsNotNone(parsed, f"not recognised: {text}")
                self.assertEqual(parsed["target"], number)
                # Whatever the model receives must still carry the request.
                self.assertTrue(parsed["message"].strip())
                self.assertNotIn("terminal", parsed["message"].lower())

    def test_an_untargeted_request_is_left_alone(self):
        for text in ("open top", "what is the weather", "open terminal 2"):
            with self.subTest(text=text):
                self.assertIsNone(parse_terminal_target(text, "en"))


if __name__ == "__main__":
    unittest.main()


class ChatRouteTargetingTests(unittest.TestCase):
    """End-to-end: what the browser sends must reach the agent's tool context.

    The parser unit tests prove the number is extracted; this proves the number
    and the session id survive the route and reach `terminal_command`, which is
    where the earlier failure lived: the turn was pinned, but the model was told
    nothing, so it opened a new terminal instead.
    """

    def _post_chat(self, payload):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        import app as app_module

        db = MagicMock()
        db.get_user.return_value = {"id": "alice", "name": "Alice"}
        db.all_settings.return_value = {"provider": "openai", "model": "test"}
        db.get_conversation.return_value = {"id": "conversation", "user_id": "alice", "skill": "shell"}
        db.list_messages.return_value = []
        skills = MagicMock()
        skills.select.return_value = SimpleNamespace(model="", tools=["terminal_command"])
        skills.build_system_prompt.return_value = "test"
        engine = MagicMock()
        engine.stream.return_value = iter([{"type": "done", "usage": {}}])
        captured = {}

        def capture_engine(_name, ctx):
            captured["ctx"] = ctx
            return engine

        with patch.object(app_module, "get_db", return_value=db), \
             patch.object(app_module, "get_skill_manager", return_value=skills), \
             patch.object(app_module, "bearer_for_api", return_value=None), \
             patch.object(app_module, "is_subscription_access", return_value=False), \
             patch.object(app_module, "ProviderManager"), \
             patch.object(app_module, "make_registry"), \
             patch.object(app_module, "build_engine", side_effect=capture_engine), \
             patch.object(app_module.config, "MEMORY_ENABLED", False), \
             patch.object(app_module.config, "MEMORY_SUMMARIZE", False):
            client = app_module.create_app().test_client()
            with client.session_transaction() as session:
                session["user_id"] = "alice"
            response = client.post("/api/chat", json=payload)
            self.assertEqual(response.status_code, 200)
            response.get_data(as_text=True)
        return captured.get("ctx")

    def test_a_targeted_turn_pins_the_named_window_and_warns_against_a_new_one(self):
        ctx = self._post_chat({
            "message": "open top",
            "conversation_id": "conversation",
            "focused_terminal": "SECOND-SESSION-ID",
            "terminal_target": 4,
            "terminal_map": ["first", "second", "third", "SECOND-SESSION-ID"],
            "window_context": '[Open windows: terminal #4 "Terminal SECONDS" (terminal, focused)]',
        })
        self.assertIsNotNone(ctx, "the route did not build an agent context")
        # What the tool actually resolves against.
        self.assertEqual(ctx.focused_terminal, "second-session-id")
        self.assertEqual(list(ctx.terminal_map), ["first", "second", "third", "second-session-id"])
        # And the model is told not to substitute a fresh terminal.
        self.assertIn("terminal #4", ctx.system_prompt)
        self.assertIn("never open an additional terminal", ctx.system_prompt)
        # The note is model-facing context, never stored history.
        self.assertEqual(ctx.history, [])

    def test_an_untargeted_turn_carries_no_note(self):
        ctx = self._post_chat({
            "message": "open top",
            "conversation_id": "conversation",
            "focused_terminal": "abcdef1234567890",
            "terminal_map": ["abcdef1234567890"],
        })
        self.assertNotIn("never open an additional terminal", ctx.system_prompt)
        # The focused window is still offered, as before.
        self.assertEqual(ctx.focused_terminal, "abcdef1234567890")
