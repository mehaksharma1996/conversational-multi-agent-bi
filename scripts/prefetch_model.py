"""Download the configured embedding model into the local cache.

Run once while online so later runs can use ``HF_HUB_OFFLINE=1``::

    python -m scripts.prefetch_model
    docker compose run --rm api python -m scripts.prefetch_model

The model and revision are exactly what the application will load, so the cache
entry is the one used at runtime.
"""

from __future__ import annotations

import sys

from config.settings import get_settings
from src.documents.embedding import EmbeddingError, SentenceTransformerEmbedder


def main() -> int:
    settings = get_settings()
    embedder = SentenceTransformerEmbedder(settings.embedding_model)
    try:
        vectors = embedder.embed_texts(["prefetch"])
    except EmbeddingError as exc:
        print(f"Could not load the embedding model: {exc}", file=sys.stderr)
        return 1
    revision = embedder.revision or "local path"
    print(
        f"Embedding model ready: {embedder.model_name} "
        f"(revision {revision}, {len(vectors[0])} dimensions)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
