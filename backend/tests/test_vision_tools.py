"""Chat image attachments: storage, capability check, fallback description."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import vision_tools


def _jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"x" * 32


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            DATA_DIR=Path(self.tmp.name),
            CHAT_IMAGE_TTL_SECONDS=3600,
            CHAT_IMAGES_USER_MAX=20,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_keeps_the_mime(self):
        token = vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg")
        found = vision_tools.read_image(self.config, "alice", token)
        self.assertIsNotNone(found)
        data, mime = found
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_another_user_cannot_read_the_image(self):
        token = vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg")
        self.assertIsNone(vision_tools.read_image(self.config, "bob", token))

    def test_a_bad_token_is_refused(self):
        self.assertIsNone(vision_tools.read_image(self.config, "alice", "../../etc/passwd"))
        self.assertIsNone(vision_tools.read_image(self.config, "alice", "not-a-token"))

    def test_an_expired_image_is_gone(self):
        token = vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg")
        target = next((Path(self.tmp.name) / "chat_images" / "alice").glob(f"{token}.*"))
        os.utime(target, (1, 1))
        self.assertIsNone(vision_tools.read_image(self.config, "alice", token))

    def test_an_unsupported_mime_is_refused(self):
        with self.assertRaises(ValueError):
            vision_tools.store_image(self.config, "alice", b"x", "application/pdf")


class VisionCapabilityTests(unittest.TestCase):
    def test_matching_is_case_insensitive_substring(self):
        patterns = ["gpt-4o", "llama3.2-vision"]
        self.assertTrue(vision_tools.is_vision_capable("GPT-4o-mini-2024-07-18", patterns))
        self.assertTrue(vision_tools.is_vision_capable("llama3.2-vision:11b", patterns))
        self.assertFalse(vision_tools.is_vision_capable("gpt-3.5-turbo", patterns))

    def test_an_empty_model_is_never_capable(self):
        self.assertFalse(vision_tools.is_vision_capable("", ["gpt-4o"]))
        self.assertFalse(vision_tools.is_vision_capable(None, ["gpt-4o"]))


class ResolveAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            DATA_DIR=Path(self.tmp.name),
            CHAT_IMAGE_TTL_SECONDS=3600,
            CHAT_IMAGES_USER_MAX=20,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_token_string_and_an_object_both_resolve(self):
        first = vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg")
        second = vision_tools.store_image(self.config, "alice", _jpeg(), "image/png")
        items = vision_tools.resolve_attachments(
            self.config, "alice", [first, {"token": second, "name": "shot.png"}]
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1][0], "shot.png")
        self.assertEqual(items[1][1], "image/png")

    def test_unknown_tokens_and_junk_are_dropped(self):
        first = vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg")
        items = vision_tools.resolve_attachments(
            self.config, "alice", [first, "deadbeef", 42, {"token": "nope"}]
        )
        self.assertEqual(len(items), 1)

    def test_at_most_four_images_per_turn(self):
        tokens = [vision_tools.store_image(self.config, "alice", _jpeg(), "image/jpeg") for _ in range(6)]
        items = vision_tools.resolve_attachments(self.config, "alice", tokens)
        self.assertEqual(len(items), 4)

    def test_content_parts_carry_the_text_and_each_image(self):
        items = [("a.png", "image/png", b"\x89PNG")]
        parts = vision_tools.image_content_parts("what is this?", items)
        self.assertEqual(parts[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))


class FallbackDescriptionTests(unittest.TestCase):
    def test_no_vision_model_reports_a_reason(self):
        config = SimpleNamespace(VISIO_PROVIDER="ollama", VISIO_MODEL="", VISIO_ENABLED=True, VISIO_CAMERA="")
        text, reason = vision_tools.describe_chat_images(
            config, {"visio_model": "", "visio_provider": "ollama"}, [("a.jpg", "image/jpeg", _jpeg())], "q"
        )
        self.assertIsNone(text)
        self.assertIn("no vision model", reason)


class UploadRouteTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask

        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            DATA_DIR=Path(self.tmp.name),
            CHAT_IMAGE_TTL_SECONDS=3600,
            CHAT_IMAGES_USER_MAX=20,
        )
        app = Flask(__name__)
        app.secret_key = "test"
        vision_tools.register_vision_routes(app, lambda: {"id": "alice"}, self.config)
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_returns_a_token_that_resolves(self):
        resp = self.client.post(
            "/api/vision/upload",
            data={"image": (io.BytesIO(_jpeg()), "photo.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["token"])
        self.assertIsNotNone(
            vision_tools.read_image(self.config, "alice", body["token"])
        )

    def test_a_non_image_is_rejected(self):
        resp = self.client.post(
            "/api/vision/upload",
            data={"image": (io.BytesIO(b"%PDF"), "doc.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 415)

    def test_a_missing_file_is_rejected(self):
        resp = self.client.post("/api/vision/upload", data={})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
