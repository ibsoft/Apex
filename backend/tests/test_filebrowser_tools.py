"""Tests for the window-manager file browser (list/mkdir/rename/trash/copy/
move/upload/zip/tickets) and its signed download routes."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, session

from tools.file_search import register_file_routes
from tools.filebrowser import register_filebrowser_routes


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.root = self.data_dir / "root"
        self.root.mkdir()
        self.config = SimpleNamespace(
            DATA_DIR=self.data_dir,
            SECRET_KEY="test-secret",
            BASE_URL="http://localhost",
            FILE_SEARCH_ROOTS=str(self.root),
            FM_FILE_TTL_SECONDS=3600,
        )
        self.app = Flask(__name__)
        self.app.secret_key = self.config.SECRET_KEY

        def require_user():
            return {"id": session["user_id"]} if session.get("user_id") else None

        register_file_routes(self.app, require_user, self.config)
        register_filebrowser_routes(self.app, require_user, self.config)
        self.client = self.app.test_client()
        self.login("alice")

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, user):
        with self.client.session_transaction() as sess:
            sess.clear()
            if user:
                sess["user_id"] = user

    def make(self, rel, contents=b"x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents)
        return str(path)

    def list_dir(self, rel="", user="alice"):
        self.login(user)
        return self.client.get("/api/fm/list", query_string={"path": str(self.root / rel)})

    def wait_job(self, job_id, user="alice", timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.login(user)
            data = self.client.get(f"/api/fm/jobs/{job_id}").get_json()
            if data.get("status") in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.02)
        return self.client.get(f"/api/fm/jobs/{job_id}").get_json()

    # ---- auth ---------------------------------------------------------------

    def test_all_endpoints_require_sign_in(self):
        self.login(None)
        checks = [
            ("get", "/api/fm/roots"),
            ("get", "/api/fm/list"),
            ("get", "/api/fm/stat"),
            ("post", "/api/fm/mkdir"),
            ("post", "/api/fm/rename"),
            ("post", "/api/fm/trash"),
            ("get", "/api/fm/trash"),
            ("post", "/api/fm/restore"),
            ("post", "/api/fm/trash/cleanup"),
            ("post", "/api/fm/jobs"),
            ("get", "/api/fm/jobs/abc"),
            ("post", "/api/fm/jobs/abc/cancel"),
            ("post", "/api/fm/tickets"),
            ("post", "/api/fm/zip"),
            ("post", "/api/fm/upload"),
        ]
        for method, url in checks:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 401)

    # ---- browse -------------------------------------------------------------

    def test_roots_exposes_configured_root(self):
        data = self.client.get("/api/fm/roots").get_json()
        self.assertEqual(len(data["roots"]), 1)
        self.assertEqual(data["roots"][0]["path"], str(self.root))
        self.assertIsNone(data["home"])

    def test_list_returns_sorted_entries(self):
        self.make("notes/inner.txt")
        self.make("apple.txt", b"apple")
        self.make("banana.txt", b"banana")
        data = self.list_dir().get_json()
        self.assertEqual(data["path"], str(self.root))
        names = [entry["name"] for entry in data["entries"]]
        self.assertEqual(names, ["notes", "apple.txt", "banana.txt"])
        folder = data["entries"][0]
        self.assertTrue(folder["is_dir"])
        self.assertEqual(folder["kind"], "folder")
        self.assertEqual(data["entries"][2]["size_bytes"], 6)

    def test_list_missing_directory_404(self):
        self.assertEqual(self.list_dir("missing").status_code, 404)

    def test_list_file_path_is_rejected(self):
        self.make("plain.txt")
        self.assertEqual(self.list_dir("plain.txt").status_code, 400)

    def test_stat_returns_details(self):
        path = self.make("doc.txt", b"hello world")
        data = self.client.get("/api/fm/stat", query_string={"path": path}).get_json()
        self.assertEqual(data["entry"]["size_bytes"], 11)
        self.assertEqual(data["entry"]["kind"], "text")
        self.assertEqual(data["permissions"][:2], "0o")

    # ---- path safety --------------------------------------------------------

    def test_traversal_rejected(self):
        self.assertEqual(self.list_dir("..").status_code, 400)
        self.assertEqual(self.list_dir("notes/../../etc").status_code, 400)

    def test_relative_and_outside_paths_rejected(self):
        outside = self.data_dir / "secret.txt"
        outside.write_text("nope")
        for endpoint in ("/api/fm/list", "/api/fm/stat"):
            response = self.client.get(endpoint, query_string={"path": str(outside)})
            self.assertEqual(response.status_code, 403)

    def test_symlink_escape_is_listed_but_never_operated(self):
        outside = self.data_dir / "outside.txt"
        outside.write_text("secret")
        link = self.root / "sheep"
        os.symlink(outside, link)
        data = self.list_dir().get_json()
        entry = data["entries"][0]
        self.assertTrue(entry["is_symlink"])
        self.assertEqual(entry["kind"], "link")
        response = self.client.get("/api/fm/stat", query_string={"path": str(link)})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/fm/rename", json={"path": str(link), "new_name": "goat"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/fm/trash", json={"paths": [str(link)]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["results"][0]["ok"])

    # ---- mkdir / rename -----------------------------------------------------

    def test_mkdir_and_duplicate_conflict(self):
        response = self.client.post("/api/fm/mkdir", json={"path": str(self.root / "newdir")})
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.root / "newdir").is_dir())
        response = self.client.post("/api/fm/mkdir", json={"path": str(self.root / "newdir")})
        self.assertEqual(response.status_code, 409)

    def test_mkdir_bad_name(self):
        response = self.client.post("/api/fm/mkdir", json={"path": str(self.root / " spaced")})
        self.assertEqual(response.status_code, 400)

    def test_rename_file(self):
        path = self.make("old.txt", b"data")
        response = self.client.post("/api/fm/rename", json={"path": path, "new_name": "new.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["entry"]["name"], "new.txt")
        self.assertTrue((self.root / "new.txt").exists())
        self.assertFalse((self.root / "old.txt").exists())

    def test_rename_into_existing_name_conflicts(self):
        self.make("a.txt")
        path = self.make("b.txt")
        response = self.client.post("/api/fm/rename", json={"path": path, "new_name": "a.txt"})
        self.assertEqual(response.status_code, 409)

    # ---- trash / restore / cleanup -------------------------------------------

    def test_trash_restore_cycle(self):
        path = self.make("notes/keep.txt", b"valuable")
        response = self.client.post("/api/fm/trash", json={"paths": [path]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertFalse((self.root / "notes/keep.txt").exists())
        self.assertTrue((self.data_dir / "trash/alice").exists())
        trash = self.client.get("/api/fm/trash").get_json()
        self.assertEqual(len(trash["trash"]), 1)
        self.assertEqual(trash["trash"][0]["original"], path)
        item_id = trash["trash"][0]["id"]
        response = self.client.post("/api/fm/restore", json={"ids": [item_id]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertTrue((self.root / "notes/keep.txt").exists())
        self.assertEqual(self.client.get("/api/fm/trash").get_json()["trash"], [])

    def test_restore_conflict_is_reported_and_kept(self):
        path = self.make("keep.txt", b"v")
        self.client.post("/api/fm/trash", json={"paths": [path]})
        self.make("keep.txt", b"new")
        trash = self.client.get("/api/fm/trash").get_json()
        response = self.client.post("/api/fm/restore", json={"ids": [trash["trash"][0]["id"]]})
        data = response.get_json()
        self.assertFalse(data["results"][0]["ok"])
        self.assertEqual(self.client.get("/api/fm/trash").get_json()["trash"], trash["trash"])

    def test_cleanup_removes_trashed_items(self):
        path = self.make("gone.txt")
        self.client.post("/api/fm/trash", json={"paths": [path]})
        trash = self.client.get("/api/fm/trash").get_json()
        response = self.client.post("/api/fm/trash/cleanup", json={"ids": [trash["trash"][0]["id"]]})
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertEqual(self.client.get("/api/fm/trash").get_json()["trash"], [])

    # ---- copy / move jobs -----------------------------------------------------

    def test_copy_job(self):
        self.make("notes/a.txt", b"data")
        self.make("notes/b.txt", b"more")
        (self.root / "backups").mkdir()
        response = self.client.post("/api/fm/jobs", json={
            "action": "copy", "sources": [str(self.root / "notes")], "destination": str(self.root / "backups"),
        })
        self.assertEqual(response.status_code, 200)
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["errors"], [])
        self.assertEqual((self.root / "backups/notes/a.txt").read_bytes(), b"data")
        self.assertTrue((self.root / "notes/a.txt").exists())

    def test_move_job_removes_source(self):
        src = self.make("notes/keep.txt", b"data")
        destination = str(self.root / "docs")
        (self.root / "docs").mkdir()
        response = self.client.post("/api/fm/jobs", json={
            "action": "move", "sources": [src], "destination": destination,
        })
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertFalse((self.root / "notes/keep.txt").exists())
        self.assertTrue((self.root / "docs/keep.txt").exists())

    def test_conflict_flow_409_then_resolve(self):
        self.make("a.txt", b"original")
        self.make("dest/a.txt", b"exists")
        destination = str(self.root / "dest")
        response = self.client.post("/api/fm/jobs", json={
            "action": "copy", "sources": [str(self.root / "a.txt")], "destination": destination,
        })
        self.assertEqual(response.status_code, 409)
        conflict = response.get_json()["conflicts"][0]
        self.assertEqual(conflict["source"], str(self.root / "a.txt"))
        response = self.client.post("/api/fm/jobs", json={
            "action": "copy", "sources": [conflict["source"]], "destination": destination,
            "conflicts": {conflict["source"]: "replace"},
        })
        self.assertEqual(response.status_code, 200)
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual((self.root / "dest/a.txt").read_bytes(), b"original")

    def test_keep_both_names_new_file(self):
        path = self.make("a.txt", b"new")
        self.make("dest/a.txt", b"old")
        destination = str(self.root / "dest")
        response = self.client.post("/api/fm/jobs", json={
            "action": "copy", "sources": [path], "destination": destination,
            "conflicts": {path: "keep_both"},
        })
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertTrue((self.root / "dest/a (1).txt").exists())
        self.assertEqual((self.root / "dest/a.txt").read_bytes(), b"old")

    def test_copy_into_own_subfolder_rejected(self):
        src = str(self.root / "notes")
        self.make("notes/x.txt")
        response = self.client.post("/api/fm/jobs", json={
            "action": "move", "sources": [src], "destination": src + "/inner",
        })
        self.assertEqual(response.status_code, 400)

    def test_cancel_is_accepted(self):
        folder = self.root / "many"
        folder.mkdir()
        for index in range(500):
            (folder / f"f{index}.txt").write_bytes(b"x" * 64)
        destination = str(self.root / "copied")
        (self.root / "copied").mkdir()
        response = self.client.post("/api/fm/jobs", json={
            "action": "copy", "sources": [str(folder)], "destination": destination,
        })
        job_id = response.get_json()["job_id"]
        self.client.post(f"/api/fm/jobs/{job_id}/cancel")
        job = self.wait_job(job_id)
        self.assertIn(job["status"], ("done", "cancelled"))

    # ---- tickets / zip / download -------------------------------------------

    def test_single_file_ticket_downloads(self):
        path = self.make("photo.png", b"PNG")
        data = self.client.post("/api/fm/tickets", json={"paths": [path]}).get_json()
        url = data["tickets"][str(path)]
        self.assertTrue(url.startswith("http://localhost/api/files/download/"))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PNG")

    def test_ticket_belongs_to_other_user_rejected(self):
        path = self.make("photo.png", b"PNG")
        url = self.client.post("/api/fm/tickets", json={"paths": [path]}).get_json()["tickets"][str(path)]
        self.login("bob")
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_zip_download_and_owner_enforcement(self):
        self.make("a.txt", b"first")
        self.make("b.txt", b"second")
        data = self.client.post("/api/fm/zip", json={"paths": [str(self.root / "a.txt"), str(self.root / "b.txt")]}).get_json()
        self.assertTrue(data["download_url"].startswith("http://localhost/api/fm/download/"))
        response = self.client.get(data["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"PK"))
        self.assertIn("files-", response.headers["Content-Disposition"])

    def test_zip_owned_by_other_user_rejected(self):
        self.make("a.txt", b"first")
        data = self.client.post("/api/fm/zip", json={"paths": [str(self.root / "a.txt")]}).get_json()
        self.login("bob")
        self.assertEqual(self.client.get(data["download_url"]).status_code, 403)

    def test_zip_tampered_token_rejected(self):
        self.make("a.txt", b"first")
        data = self.client.post("/api/fm/zip", json={"paths": [str(self.root / "a.txt")]}).get_json()
        response = self.client.get(data["download_url"] + "x")
        self.assertEqual(response.status_code, 404)

    def test_fm_download_ignores_token_pointing_outside_generated_dir(self):
        from tools.filebrowser import _fm_signer
        from tools.file_search import fingerprint

        crafted = (self.root / "notes.txt")
        crafted.write_text("sneaky")
        token = _fm_signer(self.config).dumps(
            {"user": "alice", "path": str(crafted), "filename": "notes.txt",
             "fingerprint": fingerprint(crafted.stat())}
        )
        response = self.client.get(f"/api/fm/download/{token}")
        self.assertEqual(response.status_code, 404)

    # ---- upload ---------------------------------------------------------------

    def test_upload_files(self):
        response = self.client.post("/api/fm/upload", data={
            "destination": str(self.root), "on_conflict": "skip",
            "files": [
                (io.BytesIO(b"one"), "one.txt"),
                (io.BytesIO(b"two"), "two.txt"),
            ],
        })
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual((self.root / "one.txt").read_bytes(), b"one")
        self.assertEqual((self.root / "two.txt").read_bytes(), b"two")

    def test_upload_creates_nested_paths(self):
        response = self.client.post("/api/fm/upload", data={
            "destination": str(self.root), "on_conflict": "skip",
            "files": [(io.BytesIO(b"deep"), "sub/deep.txt")],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertEqual((self.root / "sub/deep.txt").read_bytes(), b"deep")

    def test_upload_traversal_rejected(self):
        response = self.client.post("/api/fm/upload", data={
            "destination": str(self.root), "on_conflict": "skip",
            "files": [(io.BytesIO(b"evil"), "../../evil.txt")],
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["results"][0]["ok"])

    def test_upload_conflict_modes(self):
        self.make("clash.txt", b"old")
        for mode, expected in (("skip", b"old"), ("replace", b"new")):
            response = self.client.post("/api/fm/upload", data={
                "destination": str(self.root), "on_conflict": mode,
                "files": [(io.BytesIO(b"new"), "clash.txt")],
            })
            results = response.get_json()["results"]
            self.assertEqual((self.root / "clash.txt").read_bytes(), expected, msg=mode)
            if mode == "skip":
                self.assertFalse(results[0]["ok"])
            else:
                self.assertTrue(results[0]["ok"])
        response = self.client.post("/api/fm/upload", data={
            "destination": str(self.root), "on_conflict": "keep_both",
            "files": [(io.BytesIO(b"newest"), "clash.txt")],
        })
        self.assertTrue(response.get_json()["results"][0]["ok"])
        self.assertTrue((self.root / "clash (1).txt").exists())


if __name__ == "__main__":
    unittest.main()