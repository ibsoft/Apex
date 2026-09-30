"""VISIO never captures when disabled and never persists image bytes."""
import base64
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.base import ToolContext
from tools.visio_tools import build_visio_tools, capture_frame, describe_frame, effective_settings
from skills.manager import SkillManager, force_visio_skill, route_skill


class VisioTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(VISIO_ENABLED=False, VISIO_PROVIDER="ollama", VISIO_MODEL="vision-model", VISIO_CAMERA="", OPENAI_API_KEY="", OPENAI_BASE_URL="", OLLAMA_BASE_URL="http://localhost:11434/v1")
        self.settings = effective_settings(self.config, {})
        self.tool = build_visio_tools(self.config)[0]
        self.ctx = ToolContext(user_id="alice")
        self.patches = [patch("tools.visio_tools.effective_settings", return_value=self.settings),
                        patch("tools.visio_tools.camera_devices", return_value=[{"device": "/dev/video0", "name": "Camera", "accessible": True}]),
                        patch("tools.visio_tools.capture_frame", return_value=b"\xff\xd8jpeg"),
                        patch("tools.visio_tools.describe_frame", return_value="A dog under a car.")]
        self.runtime, self.devices, self.capture, self.describe = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_disabled_and_anonymous_never_capture(self):
        self.assertIn("disabled", self.tool.call({"action": "snapshot"}, self.ctx))
        self.assertIn("Sign in", self.tool.call({"action": "snapshot"}, ToolContext()))
        self.capture.assert_not_called()
        self.describe.assert_not_called()

    def test_status_does_not_capture_even_when_enabled(self):
        self.settings["visio_enabled"] = True
        result = json.loads(self.tool.call({"action": "status"}, self.ctx))
        self.assertEqual(result["cameras"][0]["device"], "/dev/video0")
        self.capture.assert_not_called()

    def test_fresh_frame_is_sent_to_selected_model(self):
        self.settings["visio_enabled"] = True
        for _ in range(2):
            result = json.loads(self.tool.call({"action": "snapshot", "question": "What is under the car?"}, self.ctx))
            self.assertEqual(result["description"], "A dog under a car.")
            self.assertFalse(result["snapshot_saved"])
            self.assertNotIn("jpeg", json.dumps(result))
        self.assertEqual(self.capture.call_count, 2)
        self.describe.assert_called_with(self.config, self.settings, b"\xff\xd8jpeg", "What is under the car?")

    def test_missing_disconnected_and_inaccessible(self):
        self.settings["visio_enabled"] = True
        self.devices.return_value = []
        self.assertIn("No camera", self.tool.call({"action": "snapshot"}, self.ctx))
        self.devices.return_value = [{"device": "/dev/video0", "accessible": False}]
        self.assertIn("cannot access", self.tool.call({"action": "snapshot"}, self.ctx))
        self.settings["visio_camera"] = "/etc/passwd"
        self.assertIn("disconnected", self.tool.call({"action": "snapshot"}, self.ctx))
        self.capture.assert_not_called()

    def test_invalid_config_and_input_do_not_capture(self):
        self.settings["visio_enabled"] = True
        self.settings["visio_model"] = ""
        self.assertIn("Select", self.tool.call({"action": "snapshot"}, self.ctx))
        self.settings["visio_model"] = "vision"
        self.assertIn("question", self.tool.call({"action": "snapshot", "question": []}, self.ctx))
        self.settings["visio_provider"] = "openai"
        self.assertIn("OPENAI_API_KEY", self.tool.call({"action": "snapshot"}, self.ctx))
        self.capture.assert_not_called()

    def test_disable_during_capture_prevents_upload(self):
        self.settings["visio_enabled"] = True
        self.runtime.side_effect = [dict(self.settings), {**self.settings, "visio_enabled": False}]
        self.assertIn("discarded", self.tool.call({"action": "snapshot"}, self.ctx))
        self.capture.assert_called_once()
        self.describe.assert_not_called()

    def test_provider_failure_does_not_leak_payload(self):
        self.settings["visio_enabled"] = True
        self.describe.side_effect = RuntimeError("sensitive image bytes")
        output = self.tool.call({"action": "snapshot"}, self.ctx)
        self.assertIn("could not analyze", output)
        self.assertNotIn("sensitive", output)


class CaptureTests(unittest.TestCase):
    @patch("tools.visio_tools.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("tools.visio_tools.subprocess.run")
    def test_one_frame_timeout_and_no_shell(self, run, which):
        run.return_value = SimpleNamespace(returncode=0, stdout=b"\xff\xd8jpeg")
        self.assertEqual(capture_frame("/dev/video0"), b"\xff\xd8jpeg")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-frames:v") + 1], "1")
        self.assertEqual(argv[-1], "pipe:1")
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertNotIn("shell", run.call_args.kwargs)
        run.side_effect = subprocess.TimeoutExpired("ffmpeg", 15)
        with self.assertRaisesRegex(ValueError, "timed out"):
            capture_frame("/dev/video0")
        run.side_effect = None
        self.assertEqual(capture_frame("/dev/video0"), b"\xff\xd8jpeg")

    @patch("openai.OpenAI")
    def test_multimodal_request(self, client_class):
        config = SimpleNamespace(OLLAMA_BASE_URL="http://localhost:11434/v1")
        client = client_class.return_value.__enter__.return_value
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A dog."))])
        result = describe_frame(config, {"visio_provider": "ollama", "visio_model": "local-vision"}, b"frame", "Describe it")
        self.assertEqual(result, "A dog.")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "local-vision")
        self.assertEqual(request["messages"][1]["content"][1]["image_url"]["url"], "data:image/jpeg;base64," + base64.b64encode(b"frame").decode())
        self.assertIn("Do not identify", request["messages"][0]["content"])
        client_class.return_value.__exit__.assert_called_once()


class RoutingTests(unittest.TestCase):
    def test_live_camera_requests_bypass_llm(self):
        skills = SkillManager(Path(__file__).parents[1] / "skills/definitions").all()
        provider = MagicMock()
        for text in ("What do you see now?", "take a snapshot", "look through my camera", "Τι βλέπεις τώρα;"):
            self.assertEqual(route_skill(text, skills, provider), "VISIO")
        provider.chat_stream.assert_not_called()
        self.assertIsNone(force_visio_skill("write a python camera function", skills))
        self.assertIsNone(force_visio_skill("what do you see now", []))


if __name__ == "__main__":
    unittest.main()
