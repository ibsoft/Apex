"""Tests for document ingestion helpers."""
from __future__ import annotations

import unittest

from memory.documents import chunk_text, extract_text


class ExtractTextTests(unittest.TestCase):
    def test_extract_plain_text(self):
        text = extract_text("notes.txt", b"hello world\nsecond line")
        self.assertIn("hello world", text)

    def test_extract_json(self):
        text = extract_text("data.json", b'{"key": "value"}')
        self.assertIn('"key": "value"', text)


class ChunkTextTests(unittest.TestCase):
    def test_no_chunking_for_short_text(self):
        chunks = chunk_text("short text", chunk_size=100)
        self.assertEqual(chunks, ["short text"])

    def test_chunking_large_text(self):
        text = "\n\n".join([f"Paragraph {i} with some words here." for i in range(50)])
        chunks = chunk_text(text, chunk_size=200, overlap=20)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 200)

    def test_empty_text(self):
        self.assertEqual(chunk_text(""), [])


if __name__ == "__main__":
    unittest.main()
