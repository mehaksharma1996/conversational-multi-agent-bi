"""Bounded, content-free in-memory reuse for document embeddings."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from src.documents.embedding import TextEmbedder
from src.utils.hashing import sha256_bytes

CacheObserver = Callable[[int, int, int], None]
CacheKey = tuple[str, str, str, str]


class EmbeddingVectorCache:
    """LRU of vectors keyed by workspace, immutable model identity, and text digest.

    Source text is never retained. Workspace scope is stricter than tenant-only scope and lets
    deletion/expiry remove every derived vector without affecting another workspace.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries <= 0:
            raise ValueError("Embedding cache size must be positive.")
        self._max_entries = max_entries
        self._lock = Lock()
        self._vectors: OrderedDict[CacheKey, tuple[float, ...]] = OrderedDict()

    def get(self, key: CacheKey) -> list[float] | None:
        with self._lock:
            vector = self._vectors.get(key)
            if vector is None:
                return None
            self._vectors.move_to_end(key)
            return list(vector)

    def put(self, key: CacheKey, vector: list[float]) -> None:
        with self._lock:
            self._vectors[key] = tuple(float(value) for value in vector)
            self._vectors.move_to_end(key)
            while len(self._vectors) > self._max_entries:
                self._vectors.popitem(last=False)

    def delete_workspace(self, tenant_id: str, workspace_id: str) -> None:
        with self._lock:
            keys = [key for key in self._vectors if key[0] == tenant_id and key[1] == workspace_id]
            for key in keys:
                del self._vectors[key]

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._vectors)


class CachingTextEmbedder:
    """TextEmbedder decorator that stores vectors but never input text."""

    def __init__(
        self,
        inner: TextEmbedder,
        cache: EmbeddingVectorCache,
        *,
        tenant_id: str,
        workspace_id: str,
        model_identity: str,
        observer: CacheObserver | None = None,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._model_identity = model_identity
        self._observer = observer

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        missing: OrderedDict[str, tuple[str, list[int]]] = OrderedDict()
        hits = 0
        for position, text in enumerate(texts):
            digest = sha256_bytes(text.encode("utf-8"))
            key = self._key(digest)
            cached = self._cache.get(key)
            if cached is not None:
                results[position] = cached
                hits += 1
                continue
            if digest in missing:
                missing[digest][1].append(position)
            else:
                missing[digest] = (text, [position])

        if missing:
            generated = self._inner.embed_texts([text for text, _ in missing.values()])
            if len(generated) != len(missing):
                raise ValueError("Embedding provider returned an unexpected vector count.")
            for (digest, (_, positions)), vector in zip(missing.items(), generated, strict=True):
                self._cache.put(self._key(digest), vector)
                for position in positions:
                    results[position] = list(vector)

        if self._observer is not None:
            self._observer(hits, len(missing), self._cache.entry_count)
        if any(vector is None for vector in results):
            raise RuntimeError("Embedding cache failed to resolve every input.")
        return [vector for vector in results if vector is not None]

    def _key(self, digest: str) -> CacheKey:
        return (
            self._tenant_id,
            self._workspace_id,
            self._model_identity,
            digest,
        )
