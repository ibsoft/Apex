"""Integration test for document upload into memory."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path


class MemoryUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_data_dir = os.environ.get("DATA_DIR")
        os.environ["DEV_MODE"] = "true"
        os.environ["DATA_DIR"] = self.tmp.name

        from app import create_app
        from config import config
        from memory import store

        self.config = config
        config.DATA_DIR = Path(self.tmp.name)
        config.CHROMA_DIR = str(Path(self.tmp.name) / "chroma")
        config.MEMORY_ENABLED = True
        store.register_memory_store(None)  # force a fresh store on next call

        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        if self.orig_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.orig_data_dir
        self.tmp.cleanup()

    def test_upload_text_file(self):
        data = {"files": (io.BytesIO(b"hello world\n\nthis is a test document"), "test.txt")}
        resp = self.client.post(
            "/api/memory/upload",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertGreater(body["total"], 0)
        self.assertEqual(body["files"][0]["filename"], "test.txt")

    def test_upload_rejects_no_files(self):
        resp = self.client.post("/api/memory/upload", data={})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
