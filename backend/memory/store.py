"""ChromaDB vector memory - one collection per user, provider-agnostic
embeddings (OpenAI / Ollama / torch / local hash), with the full lifecycle:
remember, recall, search, list, forget.

Entries are plain text facts with metadata (source conversation, timestamp,
category). Recall = embedding cosine similarity over the user's collection.
"""
from __future__ import annotations

import threading
import time
import uuid
import re

import chromadb
from chromadb.config import Settings

from models.embedders import EmbeddingManager


class MemoryStore:
    def __init__(self, persist_dir: str, embed_manager: "EmbeddingManager"):
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._embed = embed_manager
        self._lock = threading.Lock()
        self._collections: dict[str, object] = {}

    # ---- collection helpers ------------------------------------------------
    def _collection_name(self, user_id: str) -> str:
        embedding_key = re.sub(r"[^a-z0-9]+", "_", self._embed.name().lower()).strip("_")
        name = f"mem_{user_id}_{embedding_key}"
        return name[:63].rstrip("_")

    def _coll(self, user_id: str):
        with self._lock:
            coll = self._collections.get(user_id)
            if coll is None:
                embedding = self._embed.get()
                name = self._collection_name(user_id)
                coll = self._client.get_or_create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                    embedding_function=embedding,
                )
                self._migrate_legacy(user_id, coll, embedding)
                self._collections[user_id] = coll
            return coll

    def _migrate_legacy(self, user_id: str, target, embedding):
        """Copy entries from the pre-embedding-specific collection once."""
        legacy_name = f"mem_{user_id}"
        if legacy_name == target.name:
            return
        try:
            legacy = self._client.get_collection(name=legacy_name)
            data = legacy.get(include=["metadatas", "documents"])
            ids = data.get("ids") or []
            documents = data.get("documents") or []
            metadatas = data.get("metadatas") or []
            if ids and not target.count():
                target.add(ids=ids, documents=documents, metadatas=metadatas)
        except Exception:
            # A missing legacy collection or an unavailable old embedder must
            # not prevent the current collection from being usable.
            return

    @property
    def embedding_name(self) -> str:
        return self._embed.name()

    # ---- write -------------------------------------------------------------
    def remember(
        self,
        user_id: str,
        text: str,
        *,
        category: str = "general",
        conversation_id: str = "",
    ) -> str:
        text = text.strip()
        if not text:
            raise ValueError("empty memory")
        mem_id = uuid.uuid4().hex
        now = time.time()
        self._coll(user_id).add(
            ids=[mem_id],
            documents=[text],
            metadatas=[
                {
                    "category": category,
                    "conversation_id": conversation_id,
                    "created_at": now,
                }
            ],
        )
        return mem_id

    def remember_many(
        self, user_id: str, items: list[tuple[str, str]], conversation_id: str = ""
    ) -> list[str]:
        """items: [(text, category)]. Returns new ids."""
        if not items:
            return []
        ids, docs, metas = [], [], []
        now = time.time()
        for text, category in items:
            if not text.strip():
                continue
            ids.append(uuid.uuid4().hex)
            docs.append(text.strip())
            metas.append(
                {
                    "category": category or "general",
                    "conversation_id": conversation_id,
                    "created_at": now,
                }
            )
        if ids:
            self._coll(user_id).add(ids=ids, documents=docs, metadatas=metas)
        return ids

    # ---- read --------------------------------------------------------------
    def recall(self, user_id: str, query: str, n: int = 5) -> list[dict]:
        n = max(1, min(n, 25))
        res = self._coll(user_id).query(
            query_texts=[query], n_results=n, include=["metadatas", "documents", "distances"]
        )
        return self._shape(res)

    def search(self, user_id: str, query: str, n: int = 10) -> list[dict]:
        return self.recall(user_id, query, n)

    def list_all(self, user_id: str) -> list[dict]:
        res = self._coll(user_id).get(include=["metadatas", "documents"])
        out = []
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        for idx, mem_id in enumerate(ids):
            out.append(
                {
                    "id": mem_id,
                    "text": str(docs[idx] or "") if idx < len(docs) else "",
                    "meta": metas[idx] if idx < len(metas) else {},
                    "score": None,
                }
            )
        return sorted(out, key=lambda m: m["meta"].get("created_at", 0), reverse=True)

    def forget(self, user_id: str, mem_ids: list[str]) -> int:
        existing = self._coll(user_id).get()["ids"]
        delete = [i for i in mem_ids if i in existing]
        if delete:
            self._coll(user_id).delete(ids=delete)
        return len(delete)

    def clear(self, user_id: str) -> int:
        ids = self._coll(user_id).get()["ids"]
        if ids:
            self._coll(user_id).delete(ids=ids)
        return len(ids)

    def count(self, user_id: str) -> int:
        return len(self._coll(user_id).get()["ids"])

    # ---- shape ---------------------------------------------------------------
    @staticmethod
    def _shape(res: dict) -> list[dict]:
        out = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, mem_id in enumerate(ids or []):
            out.append(
                {
                    "id": mem_id,
                    "text": docs[i] if i < len(docs) else "",
                    "meta": metas[i] if i < len(metas) else {},
                    "score": round(float(dists[i]), 4) if i < len(dists) else None,
                }
            )
        return out

    def close(self):
        try:
            self._client.clear_system_cache()
        except Exception:
            pass


_store: MemoryStore | None = None


def get_memory() -> MemoryStore | None:
    global _store
    from config import config

    if _store is None and config.MEMORY_ENABLED:
        _store = MemoryStore(config.CHROMA_DIR, _embed_manager())
    return _store


def _embed_manager() -> EmbeddingManager:
    from db import get_db

    db = get_db()

    def lookup(key: str):
        return db.get_setting(key)

    return EmbeddingManager(lookup)


def register_memory_store(store: MemoryStore):
    global _store
    _store = store