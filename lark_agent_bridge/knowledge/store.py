"""SQLite-backed knowledge storage and search."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .models import KnowledgeChunk, SearchHit


_QUERY_STOP_TERMS = {
    "adb",
    "mock",
    "broadcast",
    "知识库",
    "查知识",
    "如何",
    "怎么",
    "怎么样",
    "怎样",
    "能否",
    "可以",
    "请问",
    "帮我",
    "一下",
    "模拟",
    "信号",
    "命令",
    "指令",
    "广播",
    "构造",
    "造",
    "造数据",
}


class KnowledgeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def replace_source(
        self,
        *,
        source_id: str,
        source_type: str,
        title: str,
        source_ref: str,
        chunks: list[KnowledgeChunk],
        status: str = "ok",
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                insert into sources(source_id, source_type, title, source_ref, status, error, metadata, updated_at)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                    source_type=excluded.source_type,
                    title=excluded.title,
                    source_ref=excluded.source_ref,
                    status=excluded.status,
                    error=excluded.error,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    source_type,
                    title,
                    source_ref,
                    status,
                    error,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("delete from chunks where source_id = ?", (source_id,))
            for chunk in chunks:
                conn.execute(
                    """
                    insert into chunks(chunk_id, source_id, title, content, source_ref, kind, metadata, updated_at)
                    values(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.source_id,
                        chunk.title,
                        chunk.content,
                        chunk.source_ref,
                        chunk.kind,
                        json.dumps(chunk.metadata, ensure_ascii=False),
                        now,
                    ),
                )

    def list_sources(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                select s.source_id, s.source_type, s.title, s.source_ref, s.status, s.error,
                       s.metadata, s.updated_at, count(c.chunk_id) as chunk_count
                from sources s
                left join chunks c on c.source_id = s.source_id
                group by s.source_id
                order by s.source_id
                """
            ).fetchall()
        return [
            {
                "id": row["source_id"],
                "type": row["source_type"],
                "title": row["title"],
                "source_ref": row["source_ref"],
                "status": row["status"],
                "error": row["error"],
                "metadata": _json_dict(row["metadata"]),
                "updated_at": row["updated_at"],
                "chunk_count": int(row["chunk_count"] or 0),
            }
            for row in rows
        ]

    def add_chunks(
        self,
        *,
        source_id: str,
        source_type: str,
        title: str,
        source_ref: str,
        chunks: list[KnowledgeChunk],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                insert into sources(source_id, source_type, title, source_ref, status, error, metadata, updated_at)
                values(?, ?, ?, ?, 'ok', '', ?, ?)
                on conflict(source_id) do update set
                    source_type=excluded.source_type,
                    title=excluded.title,
                    source_ref=excluded.source_ref,
                    status='ok',
                    error='',
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    source_type,
                    title,
                    source_ref,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            for chunk in chunks:
                conn.execute(
                    """
                    insert or replace into chunks(chunk_id, source_id, title, content, source_ref, kind, metadata, updated_at)
                    values(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.id,
                        chunk.source_id,
                        chunk.title,
                        chunk.content,
                        chunk.source_ref,
                        chunk.kind,
                        json.dumps(chunk.metadata, ensure_ascii=False),
                        now,
                    ),
                )

    def all_chunks(self) -> list[KnowledgeChunk]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "select chunk_id, source_id, title, content, source_ref, kind, metadata from chunks"
            ).fetchall()
        return [
            KnowledgeChunk(
                id=row["chunk_id"],
                source_id=row["source_id"],
                title=row["title"],
                content=row["content"],
                source_ref=row["source_ref"],
                kind=row["kind"],
                metadata=_json_dict(row["metadata"]),
            )
            for row in rows
        ]

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        cleaned = query.strip()
        if not cleaned:
            return []
        scored: list[SearchHit] = []
        for chunk in self.all_chunks():
            score = _score_chunk(cleaned, chunk)
            if score <= 0:
                continue
            scored.append(
                SearchHit(
                    chunk_id=chunk.id,
                    source_id=chunk.source_id,
                    title=chunk.title,
                    content=chunk.content,
                    source_ref=chunk.source_ref,
                    kind=chunk.kind,
                    score=score,
                    metadata=chunk.metadata,
                )
            )
        scored.sort(key=lambda item: (-item.score, item.source_id, item.title))
        return scored[: max(1, int(limit))]

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                create table if not exists sources(
                    source_id text primary key,
                    source_type text not null,
                    title text not null,
                    source_ref text not null,
                    status text not null,
                    error text not null,
                    metadata text not null,
                    updated_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists chunks(
                    chunk_id text primary key,
                    source_id text not null,
                    title text not null,
                    content text not null,
                    source_ref text not null,
                    kind text not null,
                    metadata text not null,
                    updated_at text not null
                )
                """
            )
            conn.execute("create index if not exists idx_chunks_source on chunks(source_id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _score_chunk(query: str, chunk: KnowledgeChunk) -> float:
    title = chunk.title.casefold()
    content = chunk.content.casefold()
    haystack = f"{title}\n{content}"
    query_cf = query.casefold()
    score = 0.0
    if query_cf and query_cf in haystack:
        score += 20.0
    for term in _query_terms(query_cf):
        if not term:
            continue
        if _contains_query_term(title, term):
            score += 8.0
        if _contains_query_term(content, term):
            score += 3.0
        metadata_keywords = chunk.metadata.get("keywords")
        if isinstance(metadata_keywords, list) and _contains_query_term(
            " ".join(str(item).casefold() for item in metadata_keywords), term
        ):
            score += 4.0
    return score


def _query_terms(value: str) -> list[str]:
    terms = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", _normalize_query_text(value))
    expanded: list[str] = []
    for term in terms:
        _append_query_term(expanded, term)
        if len(term) > 4 and re.search(r"[\u4e00-\u9fff]", term):
            for index in range(0, len(term) - 1):
                _append_query_term(expanded, term[index : index + 2])
    return expanded


def query_terms(value: str) -> list[str]:
    """Return searchable non-generic terms using the same tokenizer as scoring."""
    return _query_terms(value)


def _normalize_query_text(value: str) -> str:
    text = value.casefold()
    for term in sorted(_QUERY_STOP_TERMS, key=len, reverse=True):
        if re.fullmatch(r"[a-z0-9_]+", term):
            text = re.sub(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", " ", text)
        else:
            text = text.replace(term, " ")
    return text


def _append_query_term(terms: list[str], term: str) -> None:
    cleaned = term.strip().casefold()
    if not cleaned or cleaned in _QUERY_STOP_TERMS:
        return
    if cleaned in terms:
        return
    terms.append(cleaned)


def _contains_query_term(value: str, term: str) -> bool:
    if re.fullmatch(r"[a-z0-9_]+", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value) is not None
    return term in value


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
