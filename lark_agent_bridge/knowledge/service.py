"""Knowledge-base orchestration and answer generation."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..models import BridgeConfig, KnowledgeSourceOptions, TaskResult
from .ingestors import KnowledgeIngestError, ingest_source, source_ref, source_title
from .models import KnowledgeChunk, SearchHit
from .store import KnowledgeStore


_SIMULATION_INTENT_TERMS = ("模拟", "怎么", "如何", "命令", "指令", "广播", "mock")
_SIGNAL_DOMAIN_TERMS = ("信号", "signal_", "adb", "ota", "主题", "xtheme", "pb", "proto", "bytearray", "复杂")
_MOCK_ACTION = "com.xiaopeng.guide.action.mock.datacenter"
_SIMULATION_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "ota",
        "signal": "SIGNAL_OTA_ST",
        "code": "105003",
        "title": "OTA 状态信号",
        "intro": "模拟 OTA 活动露出和升级状态，适合验证升级活动、升级状态相关卡片。",
        "keywords": ("ota", "升级", "升级状态", "ota信号"),
        "category": "ota",
        "direct": True,
        "support": "generic",
        "example": f'adb shell am broadcast -a {_MOCK_ACTION} --ei code 105003 --ei format 7 --es value "[1, 2]"',
        "format_note": 'format 7(String)，value="[campaign, upgrade]"',
    },
    {
        "id": "xtheme",
        "signal": "SIGNAL_SR_XTHEME",
        "code": "105004",
        "title": "SR 时光主题信号",
        "intro": "模拟早晨、白天、傍晚、夜晚以及白天/黑夜主题模式，适合验证主题切换展示。",
        "keywords": ("主题", "时光主题", "xtheme", "白天", "黑夜", "夜晚", "晨曦", "傍晚"),
        "category": "theme",
        "direct": True,
        "support": "custom_factory",
        "example": f'adb shell am broadcast -a {_MOCK_ACTION} --ei code 105004 --ei format 18 --es value "1,0"',
        "format_note": 'format 18(JavaObject)，value="timePeriod,themeMode"',
    },
    {
        "id": "xtheme_msg",
        "signal": "SIGNAL_SR_XTHEME_MSG",
        "code": "105009",
        "title": "当前主题及皮肤信息",
        "intro": "描述当前主题/皮肤状态，适合查状态透传；不是已验证的通用 ADB 切换模板。",
        "keywords": ("主题", "皮肤", "xtheme", "当前主题"),
        "category": "theme",
        "direct": False,
        "support": "custom_required",
        "example": "",
        "format_note": "需要结合具体消息结构补齐 format/value。",
    },
    {
        "id": "cloud_hide_sr_theme",
        "signal": "SIGNAL_CLOUD_HIDE_SR_THEME",
        "code": "102055",
        "title": "云控隐藏 SR 主题",
        "intro": "控制 SR 主题入口是否隐藏，适合排查主题入口被云控关闭的场景。",
        "keywords": ("主题", "云控", "隐藏", "hide", "sr主题"),
        "category": "theme",
        "direct": False,
        "support": "custom_required",
        "example": "",
        "format_note": "需要按对应云控信号定义确认 format/value。",
    },
    {
        "id": "cloud_hide_car_model_theme",
        "signal": "SIGNAL_CLOUD_HIDE_CAR_MODEL_THEME",
        "code": "102056",
        "title": "云控隐藏车型主题",
        "intro": "控制车型主题入口是否隐藏，适合排查车型主题入口展示问题。",
        "keywords": ("主题", "车型主题", "云控", "隐藏", "hide"),
        "category": "theme",
        "direct": False,
        "support": "custom_required",
        "example": "",
        "format_note": "需要按对应云控信号定义确认 format/value。",
    },
    {
        "id": "sr_property",
        "signal": "SIGNAL_SR_PROPERTY",
        "code": "105007",
        "title": "SR Property PB 字节信号",
        "intro": "业务发送的是 PB 序列化后的 ByteArray，通用 ADB 字符串转换不能直接构造。",
        "keywords": ("property", "属性", "pb", "proto", "bytearray", "set_property", "复杂对象"),
        "category": "complex",
        "direct": False,
        "support": "custom_required",
        "example": "",
        "format_note": "需要新增 mockDataFactory builder，或用录制文件走 record 回放。",
    },
)


class KnowledgeService:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.store = KnowledgeStore(config.knowledge.storage)

    def should_handle(self, text: str) -> bool:
        if not self.config.knowledge.enabled:
            return False
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        for prefix in self.config.knowledge.trigger_prefixes:
            if prefix and cleaned.startswith(prefix):
                return True
        lowered = cleaned.casefold()
        if "知识库" in cleaned or "查知识" in cleaned:
            return True
        if _looks_like_signal_simulation_question(cleaned):
            return True
        if "SIGNAL_" in cleaned and any(term in lowered for term in ("adb", "mock", "模拟", "指令", "命令", "广播")):
            return True
        if "adb" in lowered and any(term in cleaned for term in ("怎么", "如何", "命令", "指令", "模拟")):
            return True
        return False

    def sync_all(self) -> dict[str, Any]:
        sources = self.config.knowledge.sources
        source_results: list[dict[str, Any]] = []
        total_chunks = 0
        for source in sources:
            try:
                chunks = ingest_source(self.config, source)
                self.store.replace_source(
                    source_id=source.id,
                    source_type=source.type,
                    title=source_title(source),
                    source_ref=source_ref(source),
                    chunks=chunks,
                    metadata=_source_metadata(source),
                )
                status = "ok"
                error = ""
            except KnowledgeIngestError as exc:
                chunks = []
                status = "error"
                error = str(exc)
                self.store.replace_source(
                    source_id=source.id,
                    source_type=source.type,
                    title=source_title(source),
                    source_ref=source_ref(source),
                    chunks=[],
                    status=status,
                    error=error,
                    metadata=_source_metadata(source),
                )
            total_chunks += len(chunks)
            source_results.append(
                {
                    "id": source.id,
                    "type": source.type,
                    "status": status,
                    "error": error,
                    "chunk_count": len(chunks),
                }
            )
        return {"source_count": len(sources), "total_chunks": total_chunks, "sources": source_results}

    def add_text(
        self,
        *,
        source_id: str,
        title: str,
        content: str,
        source_ref: str = "",
        kind: str = "manual",
    ) -> dict[str, Any]:
        cleaned_source = source_id.strip() or "manual"
        cleaned_title = title.strip() or "手工知识"
        cleaned_content = content.strip()
        if not cleaned_content:
            raise ValueError("content 不能为空")
        chunk = KnowledgeChunk(
            id=f"{cleaned_source}:manual:{hashlib.sha1(cleaned_content.encode('utf-8')).hexdigest()[:16]}",
            source_id=cleaned_source,
            title=cleaned_title,
            content=cleaned_content,
            source_ref=source_ref.strip(),
            kind=kind,
            metadata={"manual": True},
        )
        self.store.add_chunks(
            source_id=cleaned_source,
            source_type=kind,
            title=cleaned_source,
            source_ref=source_ref.strip(),
            chunks=[chunk],
        )
        return chunk.to_dict()

    def register_source(
        self,
        *,
        source_id: str,
        source_type: str = "manual",
        title: str = "",
        source_ref: str = "",
    ) -> dict[str, Any]:
        cleaned_source = source_id.strip()
        if not cleaned_source:
            raise ValueError("source_id 不能为空")
        cleaned_type = source_type.strip() or "manual"
        self.store.replace_source(
            source_id=cleaned_source,
            source_type=cleaned_type,
            title=title.strip() or cleaned_source,
            source_ref=source_ref.strip(),
            chunks=[],
        )
        return {
            "id": cleaned_source,
            "type": cleaned_type,
            "title": title.strip() or cleaned_source,
            "source_ref": source_ref.strip(),
            "chunk_count": 0,
        }

    def list_sources(self) -> list[dict[str, Any]]:
        return self.store.list_sources()

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        return self.store.search(query, limit=limit or self.config.knowledge.max_hits)

    def answer(self, question: str) -> TaskResult:
        cleaned = _strip_trigger_prefix(question, self.config.knowledge.trigger_prefixes)
        if not any(source.get("chunk_count", 0) for source in self.store.list_sources()):
            self.sync_all()
        hits = self.search(cleaned, limit=self.config.knowledge.max_hits)
        simulation_result = _build_simulation_result(cleaned, hits)
        if simulation_result is not None:
            return simulation_result
        if not hits:
            return TaskResult(
                success=False,
                message="知识库里没有命中可用内容。可以先同步知识库，或补充更具体的关键词。",
                error_code="knowledge_no_hits",
                details={"mode": "knowledge_qa", "question": cleaned, "knowledge_hits": []},
            )
        message = _generic_answer(cleaned, hits)
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "knowledge_qa",
                "question": cleaned,
                "answer_type": "retrieval_summary",
                "knowledge_hits": [hit.to_dict(include_content=False) for hit in hits],
            },
        )


def _source_metadata(source: KnowledgeSourceOptions) -> dict[str, str]:
    return {
        "url": source.url,
        "path": source.path,
        "table_id": source.table_id,
        "view_id": source.view_id,
    }


def _strip_trigger_prefix(text: str, prefixes: list[str]) -> str:
    cleaned = (text or "").strip()
    for prefix in prefixes:
        if prefix and cleaned.startswith(prefix):
            return cleaned[len(prefix) :].strip()
    return cleaned


def _looks_like_signal_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    has_intent = any(term in lowered for term in _SIMULATION_INTENT_TERMS) or any(
        term in text for term in _SIMULATION_INTENT_TERMS
    )
    if not has_intent:
        return False
    return any(term in lowered for term in _SIGNAL_DOMAIN_TERMS) or any(
        term in text for term in ("信号", "主题", "复杂对象")
    )


def _build_simulation_result(question: str, hits: list[SearchHit]) -> TaskResult | None:
    matches, exact_signal = _matching_simulation_templates(question)
    if not matches:
        if _looks_like_complex_simulation_question(question):
            return TaskResult(
                success=True,
                message=_complex_simulation_policy_answer(),
                details={
                    "mode": "knowledge_qa",
                    "question": question,
                    "answer_type": "adb_simulation_policy",
                    "knowledge_hits": [hit.to_dict(include_content=False) for hit in hits],
                },
            )
        return None
    if not exact_signal and _looks_like_complex_simulation_question(question) and all(
        template.get("category") == "complex" for template in matches
    ):
        return TaskResult(
            success=True,
            message=_complex_simulation_policy_answer(),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_simulation_policy",
                "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
            },
        )
    if exact_signal and len(matches) == 1 and not bool(matches[0].get("direct")):
        return TaskResult(
            success=True,
            message=_custom_required_signal_answer(matches[0]),
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_custom_required",
                "exact_signal": exact_signal,
                "candidates": [
                    {
                        "signal": str(matches[0]["signal"]),
                        "code": str(matches[0]["code"]),
                        "title": str(matches[0]["title"]),
                        "direct": False,
                        "support": str(matches[0].get("support") or ""),
                    }
                ],
                "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
            },
        )
    if len(matches) == 1 and bool(matches[0].get("direct")):
        template = matches[0]
        message = _direct_simulation_answer(str(template["id"]))
        return TaskResult(
            success=True,
            message=message,
            details={
                "mode": "knowledge_qa",
                "question": question,
                "answer_type": "adb_signal_template",
                "exact_signal": exact_signal,
                "knowledge_hits": [
                    hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)
                ],
            },
        )
    return TaskResult(
        success=True,
        message=_simulation_candidates_answer(matches),
        details={
            "mode": "knowledge_qa",
            "question": question,
            "answer_type": "adb_signal_candidates",
            "exact_signal": exact_signal,
            "candidates": [
                {
                    "signal": str(template["signal"]),
                    "code": str(template["code"]),
                    "title": str(template["title"]),
                    "direct": bool(template.get("direct")),
                    "support": str(template.get("support") or ""),
                }
                for template in matches
            ],
            "knowledge_hits": [hit.to_dict(include_content=False) for hit in _prefer_template_hits(hits, matches)],
        },
    )


def _matching_simulation_templates(text: str) -> tuple[list[dict[str, Any]], bool]:
    lowered = text.casefold()
    exact_matches = [
        template
        for template in _SIMULATION_TEMPLATES
        if _contains_signal_token(lowered, str(template["signal"]).casefold()) or str(template["code"]) in lowered
    ]
    if exact_matches:
        return _dedupe_templates(exact_matches), True

    matches: list[dict[str, Any]] = []
    for template in _SIMULATION_TEMPLATES:
        if any(str(keyword).casefold() in lowered for keyword in template["keywords"]):
            matches.append(template)
    if "主题" in text:
        matches.extend(template for template in _SIMULATION_TEMPLATES if template["category"] == "theme")
    if "ota" in lowered:
        matches.extend(template for template in _SIMULATION_TEMPLATES if template["category"] == "ota")
    if _looks_like_complex_simulation_question(text):
        matches.extend(template for template in _SIMULATION_TEMPLATES if template["category"] == "complex")
    return _dedupe_templates(matches), False


def _looks_like_complex_simulation_question(text: str) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in ("pb", "proto", "protoobject", "bytearray", "byte array")) or any(
        term in text for term in ("复杂对象", "特殊类型")
    )


def _contains_signal_token(text: str, signal: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(signal)}(?![a-z0-9_])", text) is not None


def _dedupe_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for template in templates:
        template_id = str(template["id"])
        if template_id in seen:
            continue
        seen.add(template_id)
        deduped.append(template)
    return deduped


def _prefer_template_hits(hits: list[SearchHit], templates: list[dict[str, Any]]) -> list[SearchHit]:
    keywords: list[str] = []
    for template in templates:
        keywords.extend([str(template["signal"]), str(template["code"]), str(template["title"])])
        keywords.extend(str(keyword) for keyword in template["keywords"])
    lowered_keywords = [keyword.casefold() for keyword in keywords if keyword]
    preferred = [
        hit
        for hit in hits
        if any(keyword in (hit.title + "\n" + hit.content).casefold() for keyword in lowered_keywords)
    ]
    return preferred or hits


def _direct_simulation_answer(template_id: str) -> str:
    if template_id == "xtheme":
        return _xtheme_signal_answer()
    return _ota_signal_answer()


def _ota_signal_answer() -> str:
    return (
        "基于 SrOtaService 中的 OtaCampaign 和 OtaUpgrade 枚举，这是四种常见组合的指令：\n"
        "SIGNAL_OTA_ST 四种组合指令\n"
        "1. 无升级活动、无升级状态 [0, 0]\n"
        "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 105003 --ei format 7 --es value \"[0, 0]\"\n"
        "2. 显示升级活动、无升级状态 [1, 0]\n"
        "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 105003 --ei format 7 --es value \"[1, 0]\"\n"
        "3. 显示升级活动、显示升级状态 [1, 1]\n"
        "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 105003 --ei format 7 --es value \"[1, 1]\"\n"
        "4. 显示升级活动、视频后升级状态 [1, 2]\n"
        "adb shell am broadcast -a com.xiaopeng.guide.action.mock.datacenter --ei code 105003 --ei format 7 --es value \"[1, 2]\"\n\n"
        "参数说明：\n"
        "code: 105003 (SIGNAL_OTA_ST)\n"
        "format: 7 (String 类型)\n"
        "value: \"[campaign, upgrade]\" 格式\n"
        "OTA 参数映射：\n"
        "campaign\n"
        "含义\n"
        "0 OTA_CAMPAIGN_NONE (无活动)\n"
        "1 OTA_CAMPAIGN_SHOW (显示活动)\n"
        "upgrade\n"
        "含义\n"
        "0 OTA_UPGRADE_NONE (无升级)\n"
        "1 OTA_UPGRADE_SHOW (显示升级)\n"
        "2 OTA_UPGRADE_AFTER_VIDEO (视频后升级)"
    )


def _xtheme_signal_answer() -> str:
    return (
        "基于 DataCenterBroadcastReceiver.mockXTheme 和 signal.proto 的 JavaObject 格式，这是 SR 时光主题的常见组合指令：\n"
        "SIGNAL_SR_XTHEME 常见组合指令\n"
        "1. 早晨、白天模式 [0, 0]\n"
        f"adb shell am broadcast -a {_MOCK_ACTION} --ei code 105004 --ei format 18 --es value \"0,0\"\n"
        "2. 白天、白天模式 [1, 0]\n"
        f"adb shell am broadcast -a {_MOCK_ACTION} --ei code 105004 --ei format 18 --es value \"1,0\"\n"
        "3. 傍晚、黑夜模式 [2, 1]\n"
        f"adb shell am broadcast -a {_MOCK_ACTION} --ei code 105004 --ei format 18 --es value \"2,1\"\n"
        "4. 夜晚、黑夜模式 [3, 1]\n"
        f"adb shell am broadcast -a {_MOCK_ACTION} --ei code 105004 --ei format 18 --es value \"3,1\"\n\n"
        "参数说明：\n"
        "code: 105004 (SIGNAL_SR_XTHEME)\n"
        "format: 18 (JavaObject 类型)\n"
        "value: \"timePeriod,themeMode\" 格式\n"
        "timePeriod\n"
        "含义\n"
        "0 早晨\n"
        "1 白天\n"
        "2 傍晚\n"
        "3 夜晚\n"
        "themeMode\n"
        "含义\n"
        "0 白天模式\n"
        "1 黑夜模式"
    )


def _custom_required_signal_answer(template: dict[str, Any]) -> str:
    signal = str(template["signal"])
    code = str(template["code"])
    title = str(template["title"])
    intro = str(template["intro"])
    return (
        f"{title}：{signal} ({code}) 当前不能生成通用 ADB 模拟命令。\n"
        f"{intro}\n\n"
        "原因：DataCenterBroadcastReceiver 的通用分支只把 value 字符串转换为基础类型；"
        "ByteArray/PB、ProtoObject、任意 JavaObject 需要源码里的 mockDataFactory 自定义构造对象。"
        "如果没有对应 factory，直接拼 `--ei format 10 --es value ...` 这类命令不会得到业务需要的 PB 对象。\n\n"
        "可行处理：\n"
        "1. 常用场景：新增 mockDataFactory 映射，根据简单 value 构造 PB/对象，再写入 dataCenter.mockSignal。\n"
        "2. 真实数据复现：用 ReplayReceiver/ProtocolFile 的 record 回放链路，把录制到的 ByteArray 按原始字节回灌。\n"
        "3. 知识库回答：没有源码 factory 或录制样本时，只能说明需要自定义，不能输出看似可执行的伪命令。"
    )


def _complex_simulation_policy_answer() -> str:
    return (
        "ADB 模拟要先按源码能力分层，不能只看 signal.proto 的 code/format：\n"
        "1. 基础类型可直接走通用广播：Int32、Int64、Float、Double、String、Boolean、Int32Array、FloatArray、DoubleArray。\n"
        f"示例形态：adb shell am broadcast -a {_MOCK_ACTION} --ei code <code> --ei format <format> --es value <value>\n"
        "2. 已有 mockDataFactory 的特殊对象可以给确定模板：例如 SIGNAL_SR_XTHEME、施工/障碍预警这类源码里有 builder 的信号。\n"
        "3. ByteArray/PB、ProtoObject、任意 JavaObject 没有 factory 时不能通用模拟；value 字符串无法自动变成目标 PB 对象。\n"
        "4. 这类信号的解决方式是新增 mockDataFactory 自定义 builder，或使用 ReplayReceiver/ProtocolFile 的 record 回放链路灌真实 ByteArray。\n\n"
        "知识库输出策略：只对基础类型或已验证 factory 输出 ADB 命令；其他复杂类型明确提示“需要自定义”，不要生成看似通用的 PB ADB 命令。"
    )


def _simulation_candidates_answer(matches: list[dict[str, Any]]) -> str:
    if len(matches) == 1:
        lines = ["命中一个可能的 ADB 模拟信号，但当前还没有验证过的通用 ADB 模板："]
    elif all(template.get("category") == "theme" for template in matches):
        lines = ["命中多个可能的主题模拟信号，先按用途选一个："]
    else:
        lines = ["命中多个可能的 ADB 模拟信号，先按用途选一个："]
    for index, template in enumerate(matches, start=1):
        signal = str(template["signal"])
        code = str(template["code"])
        title = str(template["title"])
        intro = str(template["intro"])
        format_note = str(template["format_note"])
        lines.append(f"{index}. {title}：{signal} ({code})\n{intro}\n{format_note}")
        example = str(template.get("example") or "")
        if example:
            lines.append(f"示例：{example}")
        lines.append(f"继续发：知识库 模拟 {signal}")
    return "\n\n".join(lines)


def _generic_answer(question: str, hits: list[SearchHit]) -> str:
    lines = [f"知识库命中 {len(hits)} 条，按相关性列出："]
    for index, hit in enumerate(hits, start=1):
        preview = _preview(hit.content)
        ref = f" 来源：{hit.source_ref}" if hit.source_ref else ""
        lines.append(f"{index}. {hit.title}{ref}\n{preview}")
    return "\n\n".join(lines)


def _preview(text: str, *, max_chars: int = 500) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"
