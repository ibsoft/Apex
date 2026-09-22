"""Tests for the EDITOR Word/Excel generation tools and download routes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, session

from skills.manager import SkillManager
from tools.base import ToolContext
from tools.editor_tools import build_editor_tools, register_editor_routes


class EditorToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.config = SimpleNamespace(
            DATA_DIR=self.data_dir,
            SECRET_KEY="test-secret",
            BASE_URL="http://localhost",
            EDITOR_ENABLED=True,
            EDITOR_FILE_TTL_SECONDS=3600,
        )
        self.ctx = ToolContext(user_id="alice")
        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY
        register_editor_routes(
            self.app,
            lambda: {"id": session["user_id"]} if "user_id" in session else None,
            self.config,
        )
        self.client = self.app.test_client()
        self.login("alice")
        self.word_tool, self.excel_tool = build_editor_tools(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def test_word_generation_and_download(self):
        spec = {
            "filename": "report.docx",
            "title": "Test Report",
            "sections": [
                {"type": "heading", "level": 1, "text": "Overview"},
                {"type": "paragraph", "text": "This is a test paragraph.", "bold": True},
                {"type": "table", "headers": ["Item", "Value"], "rows": [["A", 1], ["B", 2]]},
                {"type": "list", "items": ["bullet one", "bullet two"]},
            ],
        }
        result = json.loads(self.word_tool.call({"document": json.dumps(spec)}, self.ctx))
        self.assertEqual(result["filename"], "report.docx")
        self.assertTrue(result["download_url"].startswith("http://localhost/api/editor/download/"))
        response = self.client.get(result["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn("report.docx", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"PK"))  # .docx is a zip file

    def test_excel_generation_and_download(self):
        spec = {
            "filename": "data.xlsx",
            "sheets": [
                {
                    "name": "Sales",
                    "data": [
                        ["Product", "Q1", "Q2"],
                        ["Widget", 120, 150],
                        ["Gadget", 90, 210],
                    ],
                    "column_widths": {"A": 14, "B": 10, "C": 10},
                    "header_style": {"bold": True, "fill_color": "3366CC", "font_color": "FFFFFF"},
                    "formulas": {"D2": "=B2+C2"},
                    "charts": [
                        {
                            "type": "bar",
                            "title": "Quarterly Sales",
                            "data_range": "A1:C3",
                            "categories_from_first_column": True,
                            "series_from_first_row": True,
                            "x_axis_title": "Product",
                            "y_axis_title": "Units",
                            "position": "E5",
                        }
                    ],
                }
            ],
        }
        result = json.loads(self.excel_tool.call({"document": json.dumps(spec)}, self.ctx))
        self.assertEqual(result["filename"], "data.xlsx")
        response = self.client.get(result["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"PK"))  # .xlsx is a zip file

    def test_excel_no_data_raises(self):
        spec = {"filename": "empty.xlsx", "sheets": []}
        result = self.excel_tool.call({"document": json.dumps(spec)}, self.ctx)
        self.assertIn("Could not generate document", result)

    def test_word_default_filename(self):
        spec = {"sections": [{"type": "paragraph", "text": "Hello"}]}
        result = json.loads(self.word_tool.call({"document": json.dumps(spec)}, self.ctx))
        self.assertEqual(result["filename"], "document.docx")

    def test_requires_sign_in(self):
        anonymous = ToolContext(user_id="")
        result = self.word_tool.call({"document": json.dumps({"sections": []})}, anonymous)
        self.assertIn("Sign in", result)

    def test_invalid_json_spec(self):
        result = self.word_tool.call({"document": "not json"}, self.ctx)
        self.assertIn("Invalid JSON", result)

    def test_download_authentication(self):
        spec = {"sections": [{"type": "paragraph", "text": "Secret"}]}
        link = json.loads(self.word_tool.call({"document": json.dumps(spec)}, self.ctx))["download_url"]
        self.login(None)
        self.assertEqual(self.client.get(link).status_code, 401)
        self.login("bob")
        self.assertEqual(self.client.get(link).status_code, 403)
        self.login("alice")
        self.assertEqual(self.client.get(link).status_code, 200)
        self.assertEqual(self.client.get(link + "tampered").status_code, 404)

    def test_expired_link(self):
        spec = {"sections": [{"type": "paragraph", "text": "Old"}]}
        link = json.loads(self.word_tool.call({"document": json.dumps(spec)}, self.ctx))["download_url"]
        with patch("itsdangerous.timed.time.time", return_value=time.time() + 7200):
            self.assertEqual(self.client.get(link).status_code, 410)

    def test_disabled_by_config(self):
        config = SimpleNamespace(
            DATA_DIR=self.data_dir,
            SECRET_KEY="test-secret",
            BASE_URL="http://localhost",
            EDITOR_ENABLED=False,
            EDITOR_FILE_TTL_SECONDS=3600,
        )
        tool = build_editor_tools(config)[0]
        result = tool.call({"document": json.dumps({"sections": []})}, self.ctx)
        self.assertIn("disabled", result)

    def test_skill_discovery(self):
        manager = SkillManager(Path(__file__).resolve().parents[1] / "skills" / "definitions")
        skill = manager.get("EDITOR")
        self.assertIsNotNone(skill)
        self.assertEqual(sorted(skill.tools), ["calculate", "editor_create_excel", "editor_create_word", "web_fetch", "web_image_search", "web_search"])

    def test_path_traversal_filename(self):
        spec = {"filename": "../../../etc/passwd.docx", "sections": []}
        result = json.loads(self.word_tool.call({"document": json.dumps(spec)}, self.ctx))
        self.assertNotIn("/", result["filename"])
        self.assertTrue(result["filename"].endswith(".docx"))


if __name__ == "__main__":
    unittest.main()
