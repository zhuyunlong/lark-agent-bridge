"""Data structures for the local knowledge index."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class KnowledgeChunk:
    id: str
    source_id: str
    title: str
    content: str
    source_ref: str = ""
    kind: str = "text"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "title": self.title,
            "content": self.content,
            "source_ref": self.source_ref,
            "kind": self.kind,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    source_id: str
    title: str
    content: str
    source_ref: str = ""
    kind: str = "text"
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "title": self.title,
            "source_ref": self.source_ref,
            "kind": self.kind,
            "score": self.score,
            "metadata": self.metadata,
        }
        if include_content:
            payload["content"] = self.content
        return payload
