"""Retrieval over embeddings from any OpenAI-compatible endpoint.

**This is the reuse path.** Where an institution already runs an embedding model
— and many do, alongside the chat model they had to stand up anyway — the bridge
should use it rather than fall back to word matching. Same environment contract
as the chat provider (`OPENAI_BASE_URL`, `OPENAI_API_KEY`), so no new
infrastructure and nothing vendor-specific: set `EMBEDDING_MODEL` and this
becomes available.

Why it matters, measured on a real course (2026-08-25, 685 chunks):

> "Worum geht es in dieser Veranstaltung?" shares **zero** tokens with any
> course description — descriptions say "Weiterbildung", "digitale Lehre", "KI",
> never "Veranstaltung". The lexical fallback cannot answer it at any tuning.
> `bge-m3` scores those same two strings at 0.416 cosine similarity.

The fallback stays, and stays the default. An institution with no embedding
model still gets working retrieval — worse retrieval, and the capability
handshake says which one is in use, but the demo runs and the LMS adapters do
not change. That is the point of the seam: **better where the resources exist,
still functional where they do not.**

Chunks are embedded at index time and cached in the store, so a query costs one
embedding call. Vectors are compared by cosine similarity in pure Python: at a
few thousand chunks per course that is fast enough, and it avoids a numpy
dependency for a prototype. A production deployment should put a real vector
store behind this same interface — which is exactly what ByCS's
`local_ai_content` does with Qdrant, and why the interface matters more than
this implementation.
"""

from __future__ import annotations

import json
import math
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from ..contract import IndexRequest, RetrievalProvider, Source
from .builtin_retrieval import chunk

# Batch size for the embeddings call. Large enough that a 685-chunk course is a
# handful of requests, small enough not to trip request size limits.
_BATCH = 64


class EmbeddingRetrieval(RetrievalProvider):
    name = "embeddings"

    def __init__(
        self,
        store_path: str | Path | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        min_similarity: float = 0.35,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("EMBEDDING_MODEL", "")
        self.timeout = timeout
        # Cosine similarity below this is treated as "not relevant". Embeddings
        # score everything against everything, so without a floor every query
        # returns its k nearest chunks however unrelated they are — the same
        # failure the lexical provider had, in a different coordinate system.
        self.min_similarity = float(
            os.environ.get("EMBEDDING_MIN_SIMILARITY", min_similarity)
        )
        self._lock = threading.Lock()
        self._path = Path(store_path) if store_path else None
        self._store: dict[str, list[dict]] = {}
        if self._path and self._path.exists():
            self._load()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    # -- persistence --

    def _load(self) -> None:
        try:
            self._store = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._store = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._store, ensure_ascii=False), encoding="utf-8"
        )

    # -- embeddings --

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of strings, batching to keep requests a sane size."""
        if not self.configured:
            raise RuntimeError(
                "Embedding provider not configured: set OPENAI_BASE_URL and "
                "EMBEDDING_MODEL (see .env.example)"
            )
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH):
            batch = texts[i : i + _BATCH]
            body = json.dumps({"model": self.model, "input": batch}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/embeddings",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"embeddings {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(
                    f"cannot reach {self.base_url}: {e.reason}"
                ) from e
            # The API may return items out of order; `index` is authoritative.
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(item["embedding"] for item in items)
        return out

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if not na or not nb:
            return 0.0
        return dot / (na * nb)

    # -- interface --

    def index(self, req: IndexRequest) -> int:
        pieces: list[dict] = []
        for doc in req.documents:
            for piece in chunk(doc.text):
                pieces.append(
                    {
                        "activity_ref": doc.activity_ref,
                        "title": doc.title,
                        "locator": doc.locator,
                        "text": piece,
                    }
                )
        if not pieces:
            return 0

        for entry, vector in zip(pieces, self._embed([p["text"] for p in pieces])):
            entry["vector"] = vector

        with self._lock:
            if req.replace:
                self._store[req.course_ref] = pieces
            else:
                self._store.setdefault(req.course_ref, []).extend(pieces)
            self._save()
        return len(pieces)

    def _scored(self, course_ref: str, query: str, k: int) -> list[tuple[float, dict]]:
        with self._lock:
            entries = list(self._store.get(course_ref, []))
        if not entries:
            return []

        qv = self._embed([query])[0]
        scored = [
            (self._cosine(qv, e["vector"]), e)
            for e in entries
            if e.get("vector")
        ]
        scored = [(s, e) for s, e in scored if s >= self.min_similarity]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]

    def retrieve(self, course_ref: str, query: str, k: int = 4) -> list[Source]:
        return [
            Source(
                title=e["title"],
                locator=e.get("locator", ""),
                activity_ref=e["activity_ref"],
                score=round(s, 4),
            )
            for s, e in self._scored(course_ref, query, k)
        ]

    def passages(self, course_ref: str, query: str, k: int = 4) -> list[str]:
        return [e["text"] for _, e in self._scored(course_ref, query, k)]

    def count(self, course_ref: str) -> int:
        """How many chunks are stored for a course. 0 means not indexed."""
        with self._lock:
            return len(self._store.get(course_ref, []))

    def forget(self, course_ref: str) -> int:
        with self._lock:
            removed = len(self._store.pop(course_ref, []))
            self._save()
        return removed
