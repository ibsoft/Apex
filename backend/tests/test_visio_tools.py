"""VISIO captures only when enabled and saves only on explicit request."""
import base64
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.base import ToolContext
from tools.visio_tools import _frames, _frame_signer, _read_frame, build_visio_tools, capture_frame, describe_frame, effective_settings, pictures_directory, save_frame
from skills.manager import SkillManager, force_visio_skill, route_skill


class VisioTests(unittest.TestCase):
    def setUp(self):
        # SECRET_KEY and BASE_URL: a snapshot now mints a signed preview URL, so
        # a config without them raises inside the tool and the caller gets an
        # error string where the description should be.
        self.config = SimpleNamespace(VISIO_ENABLED=False, VISIO_PROVIDER="ollama", VISIO_MODEL="vision-model", VISIO_CAMERA="", OPENAI_API_KEY="", OPENAI_BASE_URL="", OLLAMA_BASE_URL="http://localhost:11434/v1", SECRET_KEY="test-secret", BASE_URL="https://apex.local")
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


class SaveTests(unittest.TestCase):
    def setUp(self):
        VisioTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        account = patch("tools.visio_tools.get_passwd_user", return_value=SimpleNamespace(home=str(self.home)))
        self.account = account.start()
        self.addCleanup(account.stop)
        self.settings["visio_enabled"] = True
        self.settings["visio_model"] = ""

    def test_save_one_and_three_without_model(self):
        self.capture.side_effect = [b"frame1", b"frame2", b"frame3", b"frame4"]
        first = json.loads(self.tool.call({"action": "save"}, self.ctx))
        batch = json.loads(self.tool.call({"action": "save", "count": 3}, self.ctx))
        self.assertEqual(first["saved_count"], 1)
        self.assertEqual(batch["saved_count"], 3)
        paths = [Path(p) for p in first["saved_paths"] + batch["saved_paths"]]
        self.assertEqual(len(set(paths)), 4)
        self.assertEqual([p.read_bytes() for p in paths], [b"frame1", b"frame2", b"frame3", b"frame4"])
        self.assertTrue(all(p.parent == self.home / "Pictures" for p in paths))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in paths))
        self.describe.assert_not_called()
        self.account.assert_called_with("alice")

    def test_partial_capture_failure_reports_saved_files(self):
        self.capture.side_effect = [b"frame1", ValueError("Camera disconnected")]
        result = json.loads(self.tool.call({"action": "save", "count": 3}, self.ctx))
        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(result["requested_count"], 3)
        self.assertIn("disconnected", result["error"])
        self.assertTrue(Path(result["saved_paths"][0]).is_file())

    def test_disabled_bad_counts_and_unknown_accounts_never_capture(self):
        for count in (0, -1, 11, True, 1.5, "3"):
            self.assertIn("count", self.tool.call({"action": "save", "count": count}, self.ctx))
        self.settings["visio_enabled"] = False
        self.assertIn("disabled", self.tool.call({"action": "save"}, self.ctx))
        self.settings["visio_enabled"] = True
        self.account.return_value = None
        result = json.loads(self.tool.call({"action": "save"}, self.ctx))
        self.assertIn("local system account", result["error"])
        self.capture.assert_not_called()

    def test_disabled_during_batch_discards_pending_frame(self):
        enabled = dict(self.settings)
        self.runtime.side_effect = [enabled, enabled, enabled, enabled, {**enabled, "visio_enabled": False}]
        result = json.loads(self.tool.call({"action": "save", "count": 3}, self.ctx))
        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(self.capture.call_count, 2)
        self.assertIn("discarded", result["error"])

    def test_localized_folder_and_symlink_escape(self):
        (self.home / ".config").mkdir()
        (self.home / ".config/user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/Εικόνες"\n')
        self.assertEqual(pictures_directory("alice"), self.home / "Εικόνες")
        (self.home / ".config/user-dirs.dirs").unlink()
        with tempfile.TemporaryDirectory() as other:
            (self.home / "Pictures").symlink_to(other, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "inside your home"):
                pictures_directory("alice")

    def test_existing_file_is_never_overwritten(self):
        directory = pictures_directory("alice")
        with patch("tools.visio_tools.uuid.uuid4") as uid, patch("tools.visio_tools.datetime") as date:
            uid.return_value.hex = "fixed"
            date.now.return_value = datetime(2026, 1, 1)
            path = Path(save_frame(directory, b"first"))
            with self.assertRaises(FileExistsError):
                save_frame(directory, b"second")
            self.assertEqual(path.read_bytes(), b"first")



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


class PreviewTests(unittest.TestCase):
    """"Show me what you see" has to put the picture on the screen, not only
    describe it. The frame reaches the browser as a signed URL over an in-memory
    store, and as an event, so the window opens whatever the model says."""

    def setUp(self):
        self.config = SimpleNamespace(VISIO_ENABLED=False, VISIO_PROVIDER="ollama", VISIO_MODEL="vision-model",
                                      VISIO_CAMERA="", OPENAI_API_KEY="", OPENAI_BASE_URL="",
                                      OLLAMA_BASE_URL="http://localhost:11434/v1",
                                      SECRET_KEY="test-secret", BASE_URL="https://apex.local")
        self.settings = effective_settings(self.config, {})
        self.settings["visio_enabled"] = True
        self.tool = build_visio_tools(self.config)[0]
        self.events: list[dict] = []
        self.ctx = ToolContext(user_id="alice", emit=self.events.append)
        self.patches = [patch("tools.visio_tools.effective_settings", return_value=self.settings),
                        patch("tools.visio_tools.camera_devices", return_value=[{"device": "/dev/video0", "name": "Camera", "accessible": True}]),
                        patch("tools.visio_tools.capture_frame", return_value=b"\xff\xd8jpeg"),
                        patch("tools.visio_tools.describe_frame", return_value="A dog under a car.")]
        for p in self.patches:
            self.addCleanup(p.stop)
            p.start()
        _frames.clear()

    def tearDown(self):
        _frames.clear()

    def test_a_snapshot_returns_a_frame_url_and_emits_it(self):
        result = json.loads(self.tool.call({"action": "snapshot", "question": "what do you see"}, self.ctx))
        self.assertEqual(result["description"], "A dog under a car.")
        self.assertIn("/api/visio/frame/", result["image_url"])
        self.assertFalse(result["snapshot_saved"], "showing a frame is not saving it")
        # The event is what opens the window; the URL in the result is only a
        # fallback for a caller that wants to render it itself.
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["type"], "visio_frame")
        self.assertEqual(self.events[0]["url"], result["image_url"])

    def test_the_url_serves_this_users_frame_and_nobody_elses(self):
        from tools.visio_tools import _frame_signer
        url = json.loads(self.tool.call({"action": "snapshot"}, self.ctx))["image_url"]
        key = url.rsplit("/", 1)[-1]
        self.assertEqual(_read_frame(_frame_signer(self.config).loads(key, max_age=300)["k"], "alice"), b"\xff\xd8jpeg")
        # Another user's session must not be able to read it, and must not be
        # able to learn that it exists either.
        self.assertIsNone(_read_frame(_frame_signer(self.config).loads(key, max_age=300)["k"], "bob"))

    def test_frames_are_held_in_memory_only(self):
        json.loads(self.tool.call({"action": "snapshot"}, self.ctx))
        # Nothing on disk: the module promises frames are written only for
        # action="save", into Pictures, by an explicit request.
        self.assertEqual(len(_frames), 1)

    def test_a_forged_or_tampered_token_names_no_frame(self):
        from itsdangerous import BadSignature
        with self.assertRaises(BadSignature):
            _frame_signer(self.config).loads("not-a-real-token")
        self.assertIsNone(_read_frame("deadbeef", "alice"))


class RoutingTests(unittest.TestCase):
    def test_live_camera_requests_bypass_llm(self):
        skills = SkillManager(Path(__file__).parents[1] / "skills/definitions").all()
        provider = MagicMock()
        for text in ("What do you see now?", "take a snapshot", "Take 3 snapshots and save them to Picture folder", "take three snapshots", "look through my camera", "Τι βλέπεις τώρα;"):
            self.assertEqual(route_skill(text, skills, provider), "VISIO")
        provider.chat_stream.assert_not_called()
        self.assertIsNone(force_visio_skill("write a python camera function", skills))
        self.assertIsNone(force_visio_skill("what do you see now", []))

    def test_show_me_what_you_seen_routes_to_visio_in_both_languages(self):
        # The imperative was not in the list, so "show me what you see" - the
        # phrasing an operator actually uses - was answered from training data
        # instead of by turning the camera on.
        skills = SkillManager(Path(__file__).parents[1] / "skills/definitions").all()
        provider = MagicMock()
        for text in ("show me what you see", "Show me what you see",
                     "please show me what you see now", "δείξε μου τι βλέπεις",
                     "δείξτε τι βλέπετε", "τι βλέπεις"):
            self.assertEqual(route_skill(text, skills, provider), "VISIO", text)
        # Anchored, unlike "what do you see": a question *about* the feature is
        # a real question and has to reach the agent.
        for text in ("how do I write a program to show me what you see",
                     "what time is it"):
            self.assertIsNot(force_visio_skill(text, skills), "VISIO", text)


if __name__ == "__main__":
    unittest.main()
