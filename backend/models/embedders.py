"""Vector embedders for ChromaDB memory - OpenAI, Ollama, torch, or local.

Selection is per-runtime setting ``embedding_backend`` (``auto`` picks the best
available: openai key > ollama server > local hash). Configurable via env at
boot (``EMBEDDING_BACKEND``/``OLLAMA_EMBED_MODEL``) or the settings API.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Callable

import numpy as np

from config import config

try:
    from chromadb.utils.embedding_functions import EmbeddingFunction  # type: ignore

    _HAS_CHROMA = True
except Exception:  # pragma: no cover
    _HAS_CHROMA = False
    EmbeddingFunction = object


def _hash_bucket(token: str, dims: int) -> int:
    return int(hashlib.md5(token.encode()).hexdigest(), 16) % dims


class HashEmbedder(EmbeddingFunction):
    """Deterministic, offline, model-free embedding (feature hashing of
    token n-grams). Good enough for keyword-ish recall when no real model is
    configured."""

    def __init__(self, dims: int = 384):
        self._dims = dims

    def __call__(self, input: list[str]) -> list[list[float]]:
        out = []
        for text in input:
            vec = np.zeros(self._dims, dtype=np.float32)
            tokens = re.findall(r"[a-z0-9_]{2,}", text.lower())
            grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
            for tok in grams:
                idx = _hash_bucket(tok, self._dims)
                sign = 1.0 if _hash_bucket(tok + "#s", 2) == 0 else -1.0
                vec[idx] += sign
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append((vec / norm).tolist())
        return out

    def name(self) -> str:
        return f"hash-{self._dims}"


class OpenAIEmbedder(EmbeddingFunction):
    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        import openai

        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def __call__(self, input: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=input)
        return [d.embedding for d in resp.data]

    def name(self) -> str:
        return f"openai:{self._model}"


class OllamaEmbedder(EmbeddingFunction):
    def __init__(self, base_url: str, model: str = "nomic-embed-text"):
        self._base = (base_url or "http://localhost:11434/v1").replace("/v1", "").rstrip("/")
        self._model = model

    def _embed_one(self, url: str, text: str) -> list[float]:
        import requests

        resp = requests.post(
            url,
            json={"model": self._model, "input": text},
            timeout=90,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "embeddings" in payload:
            return payload["embeddings"][0]
        if "embedding" in payload:
            return payload["embedding"]
        raise RuntimeError(f"ollama embed: unexpected payload {list(payload)[:5]}")

    def __call__(self, input: list[str]) -> list[list[float]]:
        # prefer modern /api/embed, fall back to legacy /api/embeddings
        urls = [f"{self._base}/api/embed", f"{self._base}/api/embeddings"]
        last_err: Exception | None = None
        for url in urls:
            try:
                return [self._embed_one(url, text) for text in input]
            except Exception as exc:
                last_err = exc
        raise last_err  # type: ignore[misc]

    def name(self) -> str:
        return f"ollama:{self._model}"


class TorchEmbedder(EmbeddingFunction):
    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2", device: str = "auto"):
        self._model_id = model
        self._device = device
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError(
                        "torch embedder requires `sentence-transformers`"
                    ) from exc
                self._model = SentenceTransformer(self._model_id, device="cuda" if self._device == "auto" and self._has_cuda() else self._device)

    @staticmethod
    def _has_cuda() -> bool:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def __call__(self, input: list[str]) -> list[list[float]]:
        self._ensure()
        return [v.tolist() for v in self._model.encode(input, convert_to_numpy=True)]

    def name(self) -> str:
        return f"torch:{self._model_id}"


# --------------------------------------------------------------------------- #
class EmbeddingManager:
    """Runtime selectable embedder with graceful fallback."""

    def __init__(self, db_lookup: Callable[[str], object]):
        self._db_lookup = db_lookup
        self._instance = None
        self._instance_key = None

    def _resolve(self):
        rt = self._db_lookup
        backend = rt("embedding_backend") or "auto"
        ordered = []
        if backend == "auto":
            ordered = ["openai", "ollama", "torch", "hash"]
        else:
            ordered = [backend]  # explicit; fall back to hash outside
        for name in ordered:
            try:
                if name == "openai" and config.OPENAI_API_KEY:
                    emb = OpenAIEmbedder(config.OPENAI_API_KEY, config.EMBEDDING_MODEL)
                    self._selfcheck(emb)
                    return emb
                if name == "ollama" and self._probe_ollama():
                    emb = OllamaEmbedder(config.OLLAMA_BASE_URL, config.OLLAMA_EMBED_MODEL)
                    self._selfcheck(emb)
                    return emb
                if name == "torch":
                    emb = TorchEmbedder()
                    self._selfcheck(emb)
                    return emb
            except Exception:
                continue
        return HashEmbedder()

    @staticmethod
    def _selfcheck(emb: EmbeddingFunction) -> bool:
        """Verify the backend actually embeds before committing to it."""
        emb(["selftest"])
        return True

    @staticmethod
    def _probe_ollama() -> bool:
        import requests

        base = (config.OLLAMA_BASE_URL or "http://localhost:11434/v1").replace("/v1", "").rstrip("/")
        resp = requests.get(f"{base}/api/tags", timeout=3)
        resp.raise_for_status()
        return True

    def get(self) -> EmbeddingFunction:
        rt = self._db_lookup
        key = str(rt("embedding_backend") or "auto")
        if self._instance is None or self._instance_key != key:
            self._instance = self._resolve()
            self._instance_key = key
        return self._instance

    def name(self) -> str:
        return self.get().name()


def default_embedder_for(cfg) -> EmbeddingFunction:  # pragma: no cover - convenience
    return HashEmbedder()


def _norm(v: list[float]) -> list[float]:
    m = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / m for x in v]