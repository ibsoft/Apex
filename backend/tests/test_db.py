"""DB message persistence never drops a turn because meta can't serialize."""
import tempfile
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import Database  # noqa: E402


class DbPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self._tmp, "apex.db"))
        self.conv = self.db.create_conversation("alice", title="First post")

    def test_meta_with_non_serializable_object_is_stored(self):
        msg_id = self.db.add_message(self.conv["id"], "assistant", "hello", {"usage": {"input": object()}})
        rows = self.db.list_messages(self.conv["id"])
        self.assertTrue(any(r["id"] == msg_id for r in rows))
        row = next(r for r in rows if r["id"] == msg_id)
        self.assertEqual(row["role"], "assistant")
        self.assertIn("usage", row["meta"])

    def test_default_meta_is_empty_object(self):
        self.db.add_message(self.conv["id"], "user", "hi")
        rows = self.db.list_messages(self.conv["id"])
        row = next(r for r in rows if r["role"] == "user")
        self.assertEqual(row["meta"], {})

    def test_title_defaults_to_new_conversation(self):
        blank = self.db.create_conversation("alice")
        self.assertEqual(blank["title"], "New conversation")


if __name__ == "__main__":
    unittest.main()