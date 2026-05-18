"""Local skill management for the report server admin UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import shutil
from pathlib import Path
from typing import Any

from .models import BridgeConfig
from .skill_registry import AUX_BUG_SKILLS, PRIMARY_BUG_SKILL_MAP, extract_skill_frontmatter


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class SkillManagerError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class SkillRecord:
    name: str
    label: str
    description: str
    role: str
    kind: str = ""
    requires_logs: bool = False
    path: str = ""
    skill_md_path: str = ""
    skill_md_exists: bool = False
    status: str = "ok"
    updated_at: str = ""
    scripts: list[str] = field(default_factory=list)
    content: str = ""

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "role": self.role,
            "kind": self.kind,
            "requires_logs": self.requires_logs,
            "path": self.path,
            "skill_md_path": self.skill_md_path,
            "skill_md_exists": self.skill_md_exists,
            "status": self.status,
            "updated_at": self.updated_at,
            "scripts": self.scripts,
        }
        if include_content:
            payload["content"] = self.content
        return payload


class SkillManager:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    @property
    def root_dir(self) -> Path:
        return self.config.workspace_root / ".ai" / "skills"

    def list_skills(self) -> list[SkillRecord]:
        records: list[SkillRecord] = []
        seen: set[str] = set()
        if self.root_dir.exists():
            for path in sorted(self.root_dir.iterdir(), key=lambda item: item.name):
                if not path.is_dir():
                    continue
                record = self._record_for(path.name, include_content=False, allow_virtual=False)
                records.append(record)
                seen.add(record.name)
        for name in PRIMARY_BUG_SKILL_MAP:
            if name not in seen:
                records.append(self._virtual_record(name))
        records.sort(key=lambda item: (_role_order(item.role), item.name))
        return records

    def get_skill(self, name: str, *, include_content: bool = True) -> SkillRecord:
        normalized = self._normalize_name(name)
        return self._record_for(normalized, include_content=include_content, allow_virtual=True)

    def create_skill(self, *, name: str, content: str = "", description: str = "", label: str = "") -> SkillRecord:
        normalized = self._normalize_name(name)
        if normalized == "general":
            raise SkillManagerError("general 是内置兜底 skill，不能创建同名目录", status_code=409)
        directory = self._skill_dir(normalized)
        if directory.exists():
            raise SkillManagerError(f"skill 已存在: {normalized}", status_code=409)
        directory.mkdir(parents=True, exist_ok=False)
        skill_md = directory / "SKILL.md"
        body = (
            content.strip() + "\n"
            if content.strip()
            else _default_skill_body(normalized, label=label, description=description)
        )
        skill_md.write_text(
            body,
            encoding="utf-8",
        )
        return self.get_skill(normalized)

    def update_skill(self, name: str, *, content: str) -> SkillRecord:
        normalized = self._normalize_name(name)
        if normalized == "general" and not self._skill_dir(normalized).exists():
            raise SkillManagerError("general 是内置兜底 skill，不能直接编辑", status_code=400)
        directory = self._skill_dir(normalized)
        if not directory.exists():
            raise SkillManagerError(f"skill 不存在: {normalized}", status_code=404)
        (directory / "SKILL.md").write_text(content.rstrip() + "\n", encoding="utf-8")
        return self.get_skill(normalized)

    def delete_skill(self, name: str) -> SkillRecord:
        normalized = self._normalize_name(name)
        if normalized == "general":
            raise SkillManagerError("general 是内置兜底 skill，不能删除", status_code=400)
        record = self.get_skill(normalized, include_content=False)
        directory = self._skill_dir(normalized)
        if not directory.exists():
            raise SkillManagerError(f"skill 不存在: {normalized}", status_code=404)
        shutil.rmtree(directory)
        return record

    def debug_skill(self, name: str, *, sample_text: str = "") -> dict[str, Any]:
        record = self.get_skill(name, include_content=True)
        frontmatter_name, _frontmatter_description = extract_skill_frontmatter(record.content)
        checks = [
            _check("name", "名称合法", True),
            _check("skill_md", "SKILL.md 存在", record.skill_md_exists, "缺少 SKILL.md，agent 无法加载这个 skill"),
            _check(
                "frontmatter_name",
                "frontmatter name 可读",
                bool(frontmatter_name) or record.status == "virtual",
                "建议补充 name 字段",
            ),
            _check(
                "description",
                "description 可读",
                bool(record.description) or record.status == "virtual",
                "建议补充 description 以提高路由可解释性",
            ),
        ]
        if record.role == "primary":
            checks.append(_check("primary_route", "已纳入 Bug 主 skill 候选", True))
        elif record.role == "auxiliary":
            checks.append(_check("auxiliary_route", "已纳入辅助 skill 展示", True))
        elif record.role == "custom":
            checks.append(_check("custom_route", "自定义 skill 已存在", True, "不会自动进入 Bug 主路由，除非路由逻辑显式支持"))
        checks.append(
            _check(
                "scripts",
                "脚本目录可读",
                True,
                "发现脚本: " + ", ".join(record.scripts) if record.scripts else "纯说明型 skill 可以没有脚本",
            )
        )
        sample = _score_sample(record, sample_text)
        return {
            "skill": record.to_dict(include_content=True),
            "checks": checks,
            "sample": sample,
            "summary": _debug_summary(checks, sample),
        }

    def _record_for(self, name: str, *, include_content: bool, allow_virtual: bool) -> SkillRecord:
        normalized = self._normalize_name(name)
        directory = self._skill_dir(normalized)
        if not directory.exists():
            if allow_virtual and normalized in PRIMARY_BUG_SKILL_MAP:
                return self._virtual_record(normalized)
            raise SkillManagerError(f"skill 不存在: {normalized}", status_code=404)
        return self._record_from_directory(directory, include_content=include_content)

    def _record_from_directory(self, directory: Path, *, include_content: bool) -> SkillRecord:
        name = directory.name
        skill_md = directory / "SKILL.md"
        content = ""
        description = ""
        frontmatter_name = ""
        status = "ok"
        if skill_md.exists():
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            frontmatter_name, description = extract_skill_frontmatter(content)
        else:
            status = "missing_skill_md"
        kind, label, requires_logs, role = self._metadata_for(name)
        scripts = _script_paths(directory)
        return SkillRecord(
            name=name,
            label=label or frontmatter_name or name,
            description=description,
            role=role,
            kind=kind,
            requires_logs=requires_logs,
            path=str(directory),
            skill_md_path=str(skill_md),
            skill_md_exists=skill_md.exists(),
            status=status,
            updated_at=_updated_at(skill_md if skill_md.exists() else directory),
            scripts=scripts,
            content=content if include_content else "",
        )

    def _virtual_record(self, name: str) -> SkillRecord:
        kind, label, requires_logs = PRIMARY_BUG_SKILL_MAP[name]
        return SkillRecord(
            name=name,
            label=label,
            description="内置路由候选；当前工作区没有对应 .ai/skills 目录。",
            role="primary",
            kind=kind,
            requires_logs=requires_logs,
            status="virtual",
        )

    def _metadata_for(self, name: str) -> tuple[str, str, bool, str]:
        if name in PRIMARY_BUG_SKILL_MAP:
            kind, label, requires_logs = PRIMARY_BUG_SKILL_MAP[name]
            return kind, label, requires_logs, "primary"
        if name in AUX_BUG_SKILLS:
            return "", "", False, "auxiliary"
        return "", "", False, "custom"

    def _skill_dir(self, name: str) -> Path:
        root = self.root_dir
        path = (root / name).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise SkillManagerError("skill 路径越界", status_code=400) from exc
        return path

    def _normalize_name(self, name: str) -> str:
        normalized = str(name or "").strip()
        if not normalized or not _SKILL_NAME_RE.fullmatch(normalized):
            raise SkillManagerError("skill 名称只能包含字母、数字、点、下划线和中划线，且长度不超过 80", status_code=400)
        return normalized


def _default_skill_body(name: str, *, label: str, description: str) -> str:
    title = label.strip() or name
    desc = description.strip() or "用于 Lark Agent Bridge 的自定义分析 skill。"
    skill_id = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "CUSTOM_SKILL"
    return (
        "---\n"
        f"name: {title}\n"
        f"description: {desc}\n"
        "argument-hint: '请输入问题现象、可选日志路径、问题时间和需要关注的关键对象。'\n"
        "user-invocable: true\n"
        f"skill_id: {skill_id}\n"
        "---\n\n"
        f"# {title}\n\n"
        "**Answer in Chinese（使用中文回答）**\n\n"
        f"{desc}\n\n"
        "## 标准执行规范\n\n"
        "### 目标\n\n"
        "说明这个 skill 要解决的问题、输入材料和最终输出。\n\n"
        "### 适用范围\n\n"
        "#### 触发关键词\n\n"
        "- 在这里列出应触发本 skill 的用户说法、日志 tag、模块名或业务关键词。\n\n"
        "#### 排除关键词\n\n"
        "- 在这里列出不应由本 skill 处理、应转交其他 skill 的场景。\n\n"
        "### 工作边界\n\n"
        "1. 只基于真实输入、工具调用、源码和日志证据输出结论。\n"
        "2. 证据不足时明确写缺口，不编造日志、源码或根因。\n"
        "3. 不把单个历史 case 的包名、PID、时间、字段写成通用规则。\n\n"
        "### 输入要求\n\n"
        "| 输入 | 必填 | 说明 |\n"
        "|---|---:|---|\n"
        "| 问题描述 | 是 | 用户现象、目标对象或排查诉求 |\n"
        "| 日志路径 | 视情况 | 需要运行证据时必须提供 |\n"
        "| 问题时间 | 推荐 | 用于锁定时间窗和减少噪声 |\n\n"
        "### 主执行链路\n\n"
        "1. 提取用户诉求、时间、日志和关键对象。\n"
        "2. 根据本 skill 范围选择工具、脚本或源码读取路径。\n"
        "3. 获取证据并区分结论、现象、缺口和推断。\n"
        "4. 先输出结论，再列证据、待确认项和建议动作。\n\n"
        "### 判断逻辑\n\n"
        "- 日志证据和源码证据互相印证时，结论可信度最高。\n"
        "- 只有单侧证据时，必须标注静态结论或日志现象。\n"
        "- 未命中证据不等于问题不存在，需说明日志覆盖和检索范围。\n\n"
        "### 工具/脚本调用规范\n\n"
        "1. 优先使用本 skill 自带脚本或明确的工具调用。\n"
        "2. 参数要区分日志路径、时间窗、tag、content、目标对象，不能混用。\n"
        "3. 失败时报告失败阶段和可复跑命令。\n\n"
        "### 证据规范\n\n"
        "1. 关键判断必须能回指到源码路径、工具结果或日志文件行号。\n"
        "2. 原始字段、时间、PID、tag 和枚举名不得改写。\n"
        "3. 重复日志要合并摘要，但保留代表样本。\n\n"
        "### 输出模板\n\n"
        "```markdown\n"
        "## 结论摘要\n"
        "- 一句话说明最可能原因、影响范围和可信度。\n\n"
        "## 关键证据\n"
        "- 列出可回溯的日志、源码或工具证据。\n\n"
        "## 最可能原因\n"
        "- 按证据强弱排序说明原因。\n\n"
        "## 待确认项\n"
        "- 列出当前材料无法证明的点。\n\n"
        "## 建议动作\n"
        "- 给出下一步可执行动作。\n"
        "```\n"
    )


def _script_paths(directory: Path) -> list[str]:
    scripts_dir = directory / "scripts"
    if not scripts_dir.exists():
        return []
    scripts: list[str] = []
    for path in sorted(scripts_dir.rglob("*")):
        if path.is_file():
            scripts.append(str(path.relative_to(directory)))
    return scripts


def _updated_at(path: Path) -> str:
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _role_order(role: str) -> int:
    return {"primary": 0, "auxiliary": 1, "custom": 2}.get(role, 3)


def _check(key: str, label: str, ok: bool, message: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "ok": ok, "message": message}


def _score_sample(record: SkillRecord, sample_text: str) -> dict[str, Any]:
    text = sample_text.strip()
    if not text:
        return {"provided": False, "score": 0, "matched_terms": [], "would_consider": False}
    haystack = text.casefold()
    candidates = [record.name, record.label, record.description, record.kind]
    matched: list[str] = []
    for candidate in candidates:
        for token in re.split(r"[\s,，/|;；:：()（）]+", candidate):
            token = token.strip().casefold()
            if len(token) < 2:
                continue
            if token in haystack and token not in matched:
                matched.append(token)
    score = min(100, len(matched) * 25)
    return {
        "provided": True,
        "score": score,
        "matched_terms": matched,
        "would_consider": score > 0,
    }


def _debug_summary(checks: list[dict[str, Any]], sample: dict[str, Any]) -> str:
    failed = [item for item in checks if not item.get("ok")]
    if failed:
        return "需要补齐: " + "、".join(str(item.get("label") or item.get("key")) for item in failed)
    if sample.get("provided") and not sample.get("would_consider"):
        return "skill 本身可读，但示例文本没有明显命中该 skill 的名称或描述。"
    return "skill 可读，基础校验通过。"
