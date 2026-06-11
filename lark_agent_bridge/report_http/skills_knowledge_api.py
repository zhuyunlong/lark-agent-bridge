"""Handler mixin: skills management and knowledge base APIs."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

from ..skill_manager import SkillManagerError
from .query import _query_int, _query_str


class SkillsKnowledgeApiMixin:
    """Skill CRUD/routing/debug and knowledge endpoints.

    Expects the composed class to provide the ``skill_manager`` and
    ``knowledge_service`` class attributes plus the response helpers from
    :class:`HttpIoAuthMixin`.
    """

    def _list_skills(self) -> list[dict[str, object]]:
        if self.skill_manager is None:
            return []
        return [item.to_dict() for item in self.skill_manager.list_skills()]

    def _get_skill(self, name: str) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        return self.skill_manager.get_skill(name).to_dict(include_content=True)

    def _create_skill(self, payload: dict[str, object]) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        name = str(payload.get("name") or "")
        content = str(payload.get("content") or "")
        description = str(payload.get("description") or "")
        label = str(payload.get("label") or "")
        return self.skill_manager.create_skill(
            name=name,
            content=content,
            description=description,
            label=label,
        ).to_dict(include_content=True)

    def _update_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        content = str(payload.get("content") or "")
        if not content.strip():
            raise ValueError("content 不能为空")
        return self.skill_manager.update_skill(name, content=content).to_dict(include_content=True)

    def _route_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        role = str(payload.get("role") or "")
        kind = str(payload.get("kind") or "")
        executor = str(payload.get("executor") or "")
        requires_logs_value = payload.get("requires_logs")
        requires_logs = requires_logs_value if isinstance(requires_logs_value, bool) else None
        return self.skill_manager.set_skill_route(
            name,
            role=role,
            kind=kind,
            executor=executor,
            requires_logs=requires_logs,
        ).to_dict(include_content=True)

    def _delete_skill(self, name: str) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        return self.skill_manager.delete_skill(name).to_dict(include_content=False)

    def _debug_skill(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        if self.skill_manager is None:
            raise SkillManagerError("skill manager not configured", status_code=503)
        sample_text = str(payload.get("sample_text") or "")
        return self.skill_manager.debug_skill(name, sample_text=sample_text)

    def _handle_skill_update(self) -> None:
        parsed = urlsplit(self.path)
        request_path = unquote(parsed.path).rstrip("/")
        if not request_path.startswith("/api/skills/"):
            self._send_json({"error": "unsupported endpoint"}, status=404)
            return
        skill_name = request_path.removeprefix("/api/skills/").strip("/")
        try:
            payload = self._read_json_body()
            self._send_json({"skill": self._update_skill(unquote(skill_name), payload)})
        except SkillManagerError as exc:
            self._send_json({"error": str(exc)}, status=exc.status_code)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def _knowledge_sources(self) -> list[dict[str, object]]:
        if self.knowledge_service is None:
            return []
        return self.knowledge_service.list_sources()

    def _knowledge_search(self, query: str) -> list[dict[str, object]]:
        if self.knowledge_service is None:
            return []
        params = parse_qs(query)
        q = _query_str(params, "q") or _query_str(params, "query")
        limit = _query_int(params, "limit", 20)
        return [hit.to_dict() for hit in self.knowledge_service.search(q, limit=limit)]

    def _knowledge_sync(self) -> dict[str, object]:
        if self.knowledge_service is None:
            return {"source_count": 0, "total_chunks": 0, "sources": []}
        return self.knowledge_service.sync_all()

    def _knowledge_register_source(self, payload: dict[str, object]) -> dict[str, object]:
        if self.knowledge_service is None:
            raise ValueError("knowledge service not configured")
        return self.knowledge_service.register_source(
            source_id=str(payload.get("source_id") or payload.get("id") or ""),
            source_type=str(payload.get("type") or "manual"),
            title=str(payload.get("title") or ""),
            source_ref=str(payload.get("source_ref") or payload.get("url") or payload.get("path") or ""),
        )

    def _knowledge_add_item(self, payload: dict[str, object]) -> dict[str, object]:
        if self.knowledge_service is None:
            raise ValueError("knowledge service not configured")
        return self.knowledge_service.add_text(
            source_id=str(payload.get("source_id") or "manual"),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or ""),
            source_ref=str(payload.get("source_ref") or ""),
        )
