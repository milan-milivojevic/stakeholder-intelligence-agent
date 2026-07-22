"""Deterministic identities for retry-safe document ingestion and Qdrant upserts."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid5

_INGESTION_NAMESPACE = UUID("413e0365-14bd-45e7-a7d5-9908934d8ce0")
_QDRANT_NAMESPACE = UUID("f47a78f8-9c5c-4df5-a753-44dce2a5bf24")


def stable_id(kind: str, *parts: str) -> str:
    """Return a readable stable opaque ID from canonical identity parts."""
    material = "\x1f".join((kind, *parts))
    return f"{kind}-{uuid5(_INGESTION_NAMESPACE, material).hex}"


def stable_document_key(*parts: str) -> str:
    """Return the non-disclosing unique key for one logical upload slot."""
    return sha256("\x1f".join(parts).encode()).hexdigest()


def qdrant_point_id(chunk_id: str) -> str:
    """Map an opaque chunk ID to a Qdrant-compatible UUID point ID."""
    return str(uuid5(_QDRANT_NAMESPACE, chunk_id))
