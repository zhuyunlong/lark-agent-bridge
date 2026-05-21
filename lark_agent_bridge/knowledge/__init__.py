"""Personal knowledge-base support for bridge QA."""

from .models import KnowledgeChunk, SearchHit
from .service import KnowledgeService

__all__ = ["KnowledgeChunk", "KnowledgeService", "SearchHit"]
