from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, session

from tools.notepad import CONTENT_START, register_notepad_routes


class NotepadRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(NOTEPAD_DOCUMENTS_DIR=self.tmp.name)
        self.app = Flask(__name__)
        self.app.secret_key = "test"

        def require_user():
            return {"id": session["user_id"]} if session.get("user_id") else None

        register_notepad_routes(self.app, require_user, self.config)
        self.client = self.app.test_client()
        self.login("alice")

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def test_save_list_open_and_download(self):
        response = self.client.post("/api/notepad/documents", json={
            "title": "Project Notes",
            "content": '<h1>Hello</h1><script>alert(1)</script><a href="javascript:bad">bad</a>',
        })
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["name"], "Project Notes.html")
        path = Path(self.tmp.name) / "alice" / result["name"]
        self.assertTrue(path.is_file())
        stored = path.read_text()
        self.assertIn(CONTENT_START, stored)
        self.assertNotIn("<script", stored)
        self.assertNotIn("javascript:", stored)
        self.assertIn("--cyan:#00e5ff", stored)

        listed = self.client.get("/api/notepad/documents").get_json()
        self.assertEqual([doc["name"] for doc in listed["documents"]], ["Project Notes.html"])
        opened = self.client.get("/api/notepad/documents/Project%20Notes.html").get_json()
        self.assertEqual(opened["title"], "Project Notes")
        self.assertIn("<h1>Hello</h1>", opened["content"])

        download = self.client.get(result["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertEqual(download.mimetype, "text/html")

    def test_documents_are_isolated_by_signed_in_user(self):
        self.client.post("/api/notepad/documents", json={"title": "Alice", "content": "private"})
        self.login("bob")
        self.assertEqual(self.client.get("/api/notepad/documents").get_json()["documents"], [])
        self.assertEqual(self.client.get("/api/notepad/documents/Alice.html").status_code, 404)

    def test_auth_traversal_and_size_are_rejected(self):
        self.login(None)
        self.assertEqual(self.client.get("/api/notepad/documents").status_code, 401)
        self.login("alice")
        self.assertEqual(self.client.get("/api/notepad/documents/..%2Fsecret.html").status_code, 404)
        response = self.client.post("/api/notepad/documents", json={"title": "Huge", "content": "x" * (2 * 1024 * 1024 + 1)})
        self.assertEqual(response.status_code, 413)

    def test_duplicate_titles_create_separate_documents(self):
        for title in ("Notes", "x" * 120):
            first = self.client.post("/api/notepad/documents", json={"title": title, "content": "first"}).get_json()
            second = self.client.post("/api/notepad/documents", json={"title": title, "content": "second"}).get_json()
            self.assertNotEqual(first["name"], second["name"])
            opened = self.client.get("/api/notepad/documents/" + first["name"]).get_json()
            self.assertEqual(opened["content"], "first")

    def test_explicit_save_updates_existing_document(self):
        first = self.client.post("/api/notepad/documents", json={"title": "Notes", "content": "first"}).get_json()
        updated = self.client.post("/api/notepad/documents", json={"name": first["name"], "title": "Renamed", "content": "second"}).get_json()
        self.assertEqual(updated["name"], first["name"])
        self.assertEqual(len(self.client.get("/api/notepad/documents").get_json()["documents"]), 1)
        self.assertEqual(self.client.get("/api/notepad/documents/" + first["name"]).get_json()["content"], "second")

    def test_invalid_payload_and_symlink_are_rejected(self):
        self.assertEqual(self.client.post("/api/notepad/documents", json=[1]).status_code, 400)
        root = Path(self.tmp.name) / "alice"
        root.mkdir(exist_ok=True)
        target = root / "target.html"
        target.write_text("private")
        (root / "link.html").symlink_to(target)
        self.assertEqual(self.client.get("/api/notepad/documents/link.html").status_code, 400)
        response = self.client.post("/api/notepad/documents", json={"name": "link.html", "content": "overwrite"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(target.read_text(), "private")

    def test_formatting_survives_save_and_open(self):
        fragment = '<span style="font-weight:bold;font-style:italic;text-decoration:line-through">Text</span><blockquote>Quote</blockquote>'
        result = self.client.post("/api/notepad/documents", json={"content": fragment}).get_json()
        self.assertEqual(self.client.get("/api/notepad/documents/" + result["name"]).get_json()["content"], fragment)

    def test_user_directory_names_do_not_collide(self):
        self.login("a/b")
        self.client.post("/api/notepad/documents", json={"content": "private"})
        self.login("a-b")
        self.assertEqual(self.client.get("/api/notepad/documents").get_json()["documents"], [])


if __name__ == "__main__":
    unittest.main()
