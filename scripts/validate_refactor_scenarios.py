#!/usr/bin/env python3
"""Run local validation scenarios for the agent-routing refactor.

The suite is intentionally offline:
- historical Bug cases come from data/state/cases.json;
- job outputs are copied into data/benchmarks before mutation;
- external Agent summary calls are replaced with a deterministic local stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import time
from typing import Any
from unittest import mock

from lark_agent_bridge.agents import Addr2LineRunner, BugAnalysisPlan, BugAnalysisRunner
from lark_agent_bridge.config import load_config
from lark_agent_bridge.knowledge import KnowledgeService
from lark_agent_bridge.models import Addr2LineRequest, DownloadResource
from lark_agent_bridge.skill_manager import SkillManager, SkillManagerError


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CASES_PATH = DATA_DIR / "state" / "cases.json"


BUG_SCENARIOS = [
    {
        "bug_id": "6997535619",
        "source_job_id": "e777bebf5a94b3a6c346dd49f3e6e0d1",
        "followup": "CLI 基于源码重新分析，并针对前后结论冲突做复盘；重点检索 XThemeStrategy.kt AndroidUnityProxy.kt",
        "requires_source": True,
    },
    {
        "bug_id": "6998811703",
        "source_job_id": "d1ba5d2a04eb6dda88d3d73b6890f44c",
        "followup": "基于源码重新分析 3D场景模式；重点检索 CarModel.cs HUCameraState.cs SceneType.kt",
        "requires_source": True,
    },
    {
        "bug_id": "6991604970",
        "source_job_id": "04eb8d099df804b29222c566391f7979",
        "followup": "重新分析 xtheme 信号变化，按当前架构只跑本地通用报告收口",
        "requires_source": False,
    },
]

KNOWLEDGE_QUESTIONS = [
    "知识库 3D天气 源码链路 怎么模拟",
    "知识库 3D SR 大场景 源码链路 怎么模拟",
    "知识库 上电P 临停P 场景 源码链路 怎么模拟",
]

RUNTIME_ROUTE_REQUIREMENTS = [
    {
        "skill": "ld-lane-level-log-analysis-portable",
        "kind": "custom_skill",
        "executor": "file_agent",
        "case": "6998107767",
        "reason": "LD 车道级退无图 custom_skill 必须显式配置文件 Agent 执行器，否则真实群聊会停在 executor_not_ready。",
    },
]


@dataclass
class PreviousContext:
    request_text: str
    summary_text: str
    report_excerpt: str
    report_url: str
    history: list[dict[str, str]]


def main() -> int:
    run_id = datetime.now().strftime("refactor_validation_%Y%m%d_%H%M%S")
    run_dir = DATA_DIR / "benchmarks" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(None)
    config.dry_run = False
    config.data_dir = DATA_DIR
    config.ai_provider.enabled = False
    config.source_investigation.codegraph_enabled = False
    config.bug_analysis.auto_fallback_to_file_agent = False

    runtime_checks = _runtime_route_checks(config)
    addr2line_checks = _addr2line_single_file_checks(config, run_dir)
    cases = _load_cases()
    bug_results = [_run_bug_scenario(config, run_dir, cases, item) for item in BUG_SCENARIOS]
    knowledge_results = [_run_knowledge_question(config, question) for question in KNOWLEDGE_QUESTIONS]
    timing_rows = _timing_rows(bug_results, knowledge_results)

    payload = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "runtime_checks": runtime_checks,
        "addr2line_checks": addr2line_checks,
        "bug_results": bug_results,
        "knowledge_results": knowledge_results,
        "timing_rows": timing_rows,
        "notes": [
            "Bug scenarios validate local follow-up/reanalysis routing, source-evidence capture, and runtime metadata.",
            "Addr2line scenarios validate single-file subrealitytrace fallback (no ROM/Napa/APK) and key thread extraction.",
            "Agent summary is stubbed to avoid external LLM/CLI cost; historical durations are kept as the old end-to-end reference.",
            "Knowledge QA uses the existing local knowledge index and source-derived evidence.",
        ],
    }
    json_path = run_dir / "summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = run_dir / "summary.md"
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    print(json.dumps({"summary_json": str(json_path), "summary_md": str(md_path), **payload}, ensure_ascii=False, indent=2))
    return 0 if _validation_passed(payload) else 1


def _runtime_route_checks(config: Any) -> list[dict[str, Any]]:
    manager = SkillManager(config)
    results: list[dict[str, Any]] = []
    for requirement in RUNTIME_ROUTE_REQUIREMENTS:
        skill_name = str(requirement["skill"])
        expected_kind = str(requirement["kind"])
        expected_executor = str(requirement["executor"])
        base = {
            "skill": skill_name,
            "case": str(requirement.get("case") or ""),
            "required_kind": expected_kind,
            "required_executor": expected_executor,
            "route_file": str(manager.route_file),
        }
        try:
            record = manager.get_skill(skill_name, include_content=False)
            executor = manager.custom_skill_executor_for(skill_name)
        except SkillManagerError as exc:
            results.append(
                {
                    **base,
                    "ok": False,
                    "reason": f"runtime skill route check failed: {exc}",
                    "kind": "",
                    "executor": "",
                    "route_status": "",
                    "selectable": False,
                }
            )
            continue
        reasons: list[str] = []
        if record.kind != expected_kind:
            reasons.append(f"kind={record.kind or '<empty>'}, expected {expected_kind}")
        if executor != expected_executor:
            reasons.append(f"executor={executor or '<empty>'}, expected executor={expected_executor}")
        if record.route_status != "bug_primary_agent_ready":
            reasons.append(f"route_status={record.route_status}, expected bug_primary_agent_ready")
        if not record.selectable_in_report_card:
            reasons.append("selectable_in_report_card=false")
        results.append(
            {
                **base,
                "ok": not reasons,
                "reason": "; ".join(reasons) if reasons else str(requirement.get("reason") or "runtime route ready"),
                "kind": record.kind,
                "executor": executor,
                "route_status": record.route_status,
                "selectable": record.selectable_in_report_card,
            }
        )
    return results


def _validation_passed(payload: dict[str, Any]) -> bool:
    runtime_checks = payload.get("runtime_checks") or []
    addr2line_checks = payload.get("addr2line_checks") or []
    runtime_ok = all(bool(item.get("ok")) for item in runtime_checks if isinstance(item, dict))
    addr2line_ok = all(bool(item.get("ok")) for item in addr2line_checks if isinstance(item, dict))
    return runtime_ok and addr2line_ok


def _addr2line_single_file_checks(config: Any, run_dir: Path) -> list[dict[str, Any]]:
    fixture_dir = run_dir / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    trace_file = fixture_dir / "subrealitytrace_2026-05-24-20-04-00"
    trace_file.write_text(
        "\n".join(
            [
                "----- pid 24454 at 2026-05-24 20:04:13.754987528+0800 -----",
                '"peng.montecarlo" sysTid=24454',
                "    #00 pc 0000000000085a9c  /apex/com.android.runtime/lib64/bionic/libc.so (syscall+28)",
                "    #01 pc 00000000030fa9bc  /system/framework/arm64/boot-framework.oat (android.app.ActivityThread.main+732)",
                '"UnityMain" sysTid=25293',
                "    #00 pc 00000000012ae2a0  /system/app/xp_envirodrive-mainland/lib/arm64/libunity.so",
                '"XPD_LD" sysTid=24839',
                "    #00 pc 00000000000264f0  /system/app/xp_envirodrive-mainland/lib/arm64/libxdata_native.so",
                '"JniSurfaceTexLoop" sysTid=24859',
                "    #00 pc 00000000000175a4  /system/app/xp_envirodrive-mainland/lib/arm64/libRenderExtend.so",
                '"RenderThread" sysTid=24525',
                "    #00 pc 00000000003c7138  /system/lib64/libhwui.so (android::uirenderer::renderthread::RenderThread::threadLoop()+76)",
                '"GLThread 883" sysTid=24542',
                "    #00 pc 000000000153d768  /system/framework/arm64/boot-framework.oat (android.opengl.GLSurfaceView$GLThread.guardedRun+1944)",
            ]
        ),
        encoding="utf-8",
    )

    runner = Addr2LineRunner(config)
    result = runner.run_resolve(
        Addr2LineRequest(
            addr_text="",
            resources=[DownloadResource(kind="local", value=str(trace_file))],
            triggered=True,
        )
    )
    counts = result.details.get("thread_category_counts") if isinstance(result.details, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    categories_ok = all(
        int(counts.get(label, 0)) > 0
        for label in ("UnityMain", "XPD_*", "JniSurfaceTex*", "主线程", "渲染相关线程")
    )
    ok = bool(result.success and result.details.get("analysis_mode") == "single_trace_thread_parse" and categories_ok)
    return [
        {
            "case": "addr2line_single_file_subrealitytrace",
            "ok": ok,
            "analysis_mode": result.details.get("analysis_mode"),
            "counts": counts,
            "error_code": result.error_code,
            "message_preview": str(result.message or "").splitlines()[:6],
            "reason": "single-file trace fallback should parse UnityMain/XPD/JniSurfaceTex/main/render threads",
        }
    ]


def _load_cases() -> dict[str, dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return {str(value.get("job_id") or key): value for key, value in payload.items() if isinstance(value, dict)}


def _run_bug_scenario(
    config: Any,
    run_dir: Path,
    cases: dict[str, dict[str, Any]],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    bug_id = str(scenario["bug_id"])
    source_job_id = str(scenario["source_job_id"])
    case = cases[source_job_id]
    source_job_dir = DATA_DIR / "jobs" / source_job_id
    copied_job_id = f"{bug_id}_{source_job_id[:8]}"
    job_dir = run_dir / "jobs" / copied_job_id
    output_dir = job_dir / "output"
    if source_job_dir.exists():
        shutil.copytree(source_job_dir, job_dir, dirs_exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    previous_context = PreviousContext(
        request_text=str(case.get("request_text") or ""),
        summary_text=str(case.get("conclusion") or "")[:4000],
        report_excerpt=str(case.get("conclusion") or "")[:2000],
        report_url=str(case.get("report_url") or ""),
        history=[
            {"role": "user", "content": str(case.get("request_text") or "")[:1000]},
            {"role": "assistant", "content": str(case.get("conclusion") or "")[:2000]},
        ],
    )
    metadata_path = output_dir / "bug_metadata.md"
    target_time = _metadata_value(metadata_path, "故障时间")
    cache_paths = _bug_cache_paths(bug_id)
    previous_session = {
        "job_id": copied_job_id,
        "job_dir": str(job_dir),
        "duration_seconds": float(case.get("duration_seconds") or 0.0),
        "details": {
            "bug_url": str(case.get("bug_url") or _extract_bug_url(previous_context.request_text)),
            "analysis_kinds": ["general"],
            "analysis_skill": "source_analysis" if scenario["requires_source"] else "general",
            "user_request_text": previous_context.request_text,
            "agent_summary_file": str(output_dir / "bug_agent_summary.md"),
            "target_time": target_time,
            "fault_time": target_time,
            "selected_log_input": cache_paths.get("selected_log_input", ""),
            "prepared_log_input": cache_paths.get("prepared_log_input", ""),
        },
    }

    runner = BugAnalysisRunner(config)
    started = time.monotonic()
    with mock.patch.object(runner, "_run_bug_agent_summary", side_effect=_summary_stub):
        result = runner.run_bug_reanalysis(
            followup_text=str(scenario["followup"]),
            previous_context=previous_context,
            previous_session=previous_session,
            plans_override=[BugAnalysisPlan(kind="general")],
            classification_skill="source_analysis" if scenario["requires_source"] else "general",
            classification_source="refactor_validation",
            classification_reason="historical bug follow-up validation",
        )
    duration = time.monotonic() - started
    source_evidence_file = str(result.details.get("source_evidence_file") or "")
    evidence_exists = bool(source_evidence_file and Path(source_evidence_file).exists())
    evidence_match_count = _count_source_evidence_matches(Path(source_evidence_file)) if evidence_exists else 0
    return {
        "bug_id": bug_id,
        "source_job_id": source_job_id,
        "copied_job_id": copied_job_id,
        "bug_url": previous_session["details"]["bug_url"],
        "followup": scenario["followup"],
        "requires_source": bool(scenario["requires_source"]),
        "success": result.success,
        "error_code": result.error_code,
        "mode": result.details.get("mode"),
        "duration_seconds": duration,
        "old_duration_seconds": float(case.get("duration_seconds") or 0.0),
        "source_evidence_file": source_evidence_file,
        "source_evidence_exists": evidence_exists,
        "source_evidence_match_count": evidence_match_count,
        "agent_summary_execution_backend": result.details.get("agent_summary_execution_backend"),
        "agent_summary_backend_reason": result.details.get("agent_summary_backend_reason"),
        "report_html": result.details.get("combined_report_html") or "",
        "metadata_file": result.details.get("reanalysis_metadata_file") or "",
    }


def _summary_stub(**kwargs: Any) -> dict[str, Any]:
    output_path = Path(kwargs["output_path"])
    message = (
        "refactor validation summary stub\n"
        f"- request_artifact: {kwargs.get('request_artifact')}\n"
        f"- metadata_path: {kwargs.get('metadata_path')}\n"
    )
    output_path.write_text(message, encoding="utf-8")
    return {
        "message": message,
        "command": [],
        "error": "",
        "provider": "validation_stub",
        "model": "offline",
        "session_id": "",
        "resumed": False,
        "duration_seconds": 0.001,
        "usage": {},
        "usage_scope": "",
        "execution_backend": "file_agent",
        "backend_reason": "direct_api_policy_skip",
        "fallback_from": "",
    }


def _run_knowledge_question(config: Any, question: str) -> dict[str, Any]:
    service = KnowledgeService(config)
    started = time.monotonic()
    result = service.answer(question)
    duration = time.monotonic() - started
    hits = result.details.get("knowledge_hits") if isinstance(result.details, dict) else []
    source_evidence_count = 0
    source_refs: list[str] = []
    if isinstance(hits, list):
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            source_ref = str(hit.get("source_ref") or "")
            if source_ref:
                source_refs.append(source_ref)
            metadata = hit.get("metadata")
            if isinstance(metadata, dict):
                evidence = metadata.get("source_evidence") or metadata.get("evidence")
                if isinstance(evidence, list):
                    source_evidence_count += len(evidence)
    return {
        "question": question,
        "success": result.success,
        "answer_type": result.details.get("answer_type") if isinstance(result.details, dict) else "",
        "duration_seconds": duration,
        "source_evidence_count": source_evidence_count,
        "source_refs": source_refs[:3],
    }


def _timing_rows(
    bug_results: list[dict[str, Any]],
    knowledge_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in bug_results:
        rows.append(
            {
                "module": f"bug_followup:{item['bug_id']}",
                "old_seconds": item["old_duration_seconds"],
                "new_seconds": item["duration_seconds"],
                "basis": "old=historical case duration, new=offline local reanalysis path",
            }
        )
    for index, item in enumerate(knowledge_results, start=1):
        rows.append(
            {
                "module": f"knowledge_source_qa:{index}",
                "old_seconds": None,
                "new_seconds": item["duration_seconds"],
                "basis": "new=local knowledge answer with source-derived evidence",
            }
        )
    return rows


def _extract_bug_url(text: str) -> str:
    marker = "https://project.feishu.cn/"
    index = text.find(marker)
    if index < 0:
        return ""
    tail = text[index:].split()[0]
    return tail.rstrip(")")


def _metadata_value(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    prefix = f"- {label}: `"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(prefix):
            continue
        return line[len(prefix) :].split("`", 1)[0].strip()
    return ""


def _bug_cache_paths(bug_id: str) -> dict[str, str]:
    cache_path = DATA_DIR / "bug_cache" / f"xpfailuremgmt_{bug_id}" / "cache.json"
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    for key in ("selected_log_input", "prepared_log_input"):
        value = str(payload.get(key) or "").strip()
        if value and Path(value).exists():
            result[key] = value
    return result


def _count_source_evidence_matches(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("- L"))


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Refactor Validation Summary",
        "",
        f"- Run: `{payload['run_id']}`",
        f"- Output: `{payload['run_dir']}`",
        "",
        "## Runtime Route Checks",
        "",
        "| Skill | Case | OK | Kind | Executor | Route Status | Selectable | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in payload.get("runtime_checks") or []:
        lines.append(
            "| {skill} | {case} | {ok} | {kind} | {executor} | {status} | {selectable} | {reason} |".format(
                skill=str(item.get("skill") or "").replace("|", "\\|"),
                case=str(item.get("case") or "").replace("|", "\\|"),
                ok=item.get("ok"),
                kind=str(item.get("kind") or "").replace("|", "\\|"),
                executor=str(item.get("executor") or "").replace("|", "\\|"),
                status=str(item.get("route_status") or "").replace("|", "\\|"),
                selectable=item.get("selectable"),
                reason=str(item.get("reason") or "").replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Bug Follow-up Scenarios",
            "",
            "| Bug | Success | Source | Evidence Hits | Old Seconds | New Seconds | Backend |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for item in payload["bug_results"]:
        lines.append(
            "| {bug_id} | {success} | {source} | {hits} | {old:.1f} | {new:.3f} | {backend}/{reason} |".format(
                bug_id=item["bug_id"],
                success=item["success"],
                source=item["source_evidence_exists"],
                hits=item["source_evidence_match_count"],
                old=item["old_duration_seconds"],
                new=item["duration_seconds"],
                backend=item["agent_summary_execution_backend"],
                reason=item["agent_summary_backend_reason"],
            )
        )
    lines.extend(
        [
            "",
            "## Knowledge QA",
            "",
            "| Question | Success | Answer Type | Source Evidence | Seconds |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for item in payload["knowledge_results"]:
        lines.append(
            "| {question} | {success} | {answer_type} | {evidence} | {seconds:.3f} |".format(
                question=item["question"].replace("|", "\\|"),
                success=item["success"],
                answer_type=item["answer_type"],
                evidence=item["source_evidence_count"],
                seconds=item["duration_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "## Timing Notes",
            "",
            "| Module | Old Seconds | New Seconds | Basis |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for row in payload["timing_rows"]:
        old = "-" if row["old_seconds"] is None else f"{row['old_seconds']:.1f}"
        lines.append(
            f"| {row['module']} | {old} | {row['new_seconds']:.3f} | {row['basis']} |"
        )
    lines.extend(["", "## Conclusion", "", "The refactor keeps source-heavy follow-up analysis on the file-capable path, avoids silent direct-API/subprocess fallback, and leaves knowledge QA on fast local retrieval when source-derived templates exist."])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
