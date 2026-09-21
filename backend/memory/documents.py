"""Document ingestion for memory: extract text and chunk it for embedding."""
from __future__ import annotations

import io
import re
from pathlib import Path


def _guess_mimetype(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".ts": "text/typescript",
        ".jsx": "text/jsx",
        ".tsx": "text/tsx",
        ".json": "application/json",
        ".csv": "text/csv",
        ".html": "text/html",
        ".htm": "text/html",
        ".xml": "text/xml",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


def extract_text(filename: str, data: bytes) -> str:
    """Best-effort text extraction from common document formats."""
    mimetype = _guess_mimetype(filename)

    if mimetype == "application/pdf":
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF support requires PyPDF2 (pip install PyPDF2)") from exc
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n\n".join(parts)

    # Everything else is treated as UTF-8 text.
    # A few formats are common enough that ignoring non-text bytes is fine.
    text = data.decode("utf-8", errors="ignore")
    if mimetype == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text


def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> list[str]:
    """Split text into overlapping chunks.

    Splits on paragraphs first, then sentences, then words, so chunks stay
    semantically coherent while staying under ``chunk_size`` characters.
    """
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # 1. Split on paragraphs.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    current = ""

    def _flush():
        nonlocal current
        if current:
            chunks.append(current.strip())
            current = ""

    def _split_by_sentences(block: str) -> list[str]:
        # Simple sentence split on punctuation followed by space or newline.
        sentences = re.split(r"(?<=[.!?])\s+", block)
        return [s.strip() for s in sentences if s.strip()]

    def _split_by_words(block: str) -> list[str]:
        words = block.split()
        out = []
        buf: list[str] = []
        length = 0
        for w in words:
            if length + len(w) + 1 > chunk_size and buf:
                out.append(" ".join(buf))
                # carry over overlap words
                carry = []
                carry_len = 0
                for cw in reversed(buf):
                    if carry_len + len(cw) + 1 > overlap:
                        break
                    carry.insert(0, cw)
                    carry_len += len(cw) + 1
                buf = carry
                length = carry_len
            buf.append(w)
            length += len(w) + 1
        if buf:
            out.append(" ".join(buf))
        return out

    for para in paragraphs:
        if len(para) > chunk_size:
            _flush()
            sentences = _split_by_sentences(para)
            sent_buf = ""
            for sent in sentences:
                if len(sent_buf) + len(sent) + 1 > chunk_size and sent_buf:
                    # sentence buffer too big; split it by words
                    chunks.extend(_split_by_words(sent_buf.strip()))
                    sent_buf = ""
                sent_buf += (" " if sent_buf else "") + sent
            if sent_buf:
                if len(sent_buf) > chunk_size:
                    chunks.extend(_split_by_words(sent_buf.strip()))
                else:
                    chunks.append(sent_buf.strip())
            continue

        if current and len(current) + len(para) + 2 > chunk_size:
            _flush()
        current += ("\n\n" if current else "") + para

    _flush()

    # Apply overlap between adjacent chunks.
    if overlap <= 0 or len(chunks) < 2:
        return chunks

    overlapped: list[str] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]
        curr = chunks[i]
        prefix = prev[-overlap:].lstrip()
        combined = (prefix + " " + curr).strip()
        if len(combined) <= chunk_size:
            overlapped.append(combined)
        else:
            overlapped.append(curr)
    return overlapped
