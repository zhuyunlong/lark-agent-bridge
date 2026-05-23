#!/usr/bin/env python3
"""Generate a Chinese HTML comparison report for Codex/Claude/oMLX bug summaries."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_REPORT_HTML = Path(
    "/Users/zhuyl/Documents/workspace/xp/guideengine/.worktrees/os6_xpdev/.ai/skills/3d-stuck-investigate/scripts/report_html.py"
)


def load_report_html_module(path: Path):
    spec = importlib.util.spec_from_file_location("shared_report_html", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load shared report html module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def classify_provider(summary: str) -> dict[str, str]:
    source_used = "源码定义" in summary or "signal.proto" in summary
    main_chain_ok = (
        "41001" in summary
        and "41005" in summary
        and "132000" in summary
        and "132001" in summary
        and ("不支持“全量感知链路完全断流”" in summary or "主链路" in summary or "频率稳定" in summary)
    )
    ld_focus = "132002" in summary or "LD Normal" in summary or "MapDataHandler" in summary
    over_strong = "直接原因" in summary
    low_conf = "低。结论基于脚本输出的缺失项" in summary or "当前无法判断" in summary
    if low_conf:
        stance = "保守"
    elif over_strong:
        stance = "激进"
    else:
        stance = "平衡"
    return {
        "source_used": "是" if source_used else "未体现",
        "main_chain_ok": "是" if main_chain_ok else "未明确",
        "ld_focus": "是" if ld_focus else "未明确",
        "stance": stance,
        "root_cause_strength": "偏强" if over_strong else ("偏弱" if low_conf else "适中"),
    }


def extract_bullets(summary: str, limit: int = 5) -> list[str]:
    bullets: list[str] = []
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            bullets.append(stripped[2:].strip())
        if len(bullets) >= limit:
            break
    return bullets


def summary_excerpt(summary: str, limit: int = 900) -> str:
    text = re.sub(r"\n{3,}", "\n\n", summary).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_report(
    *,
    module,
    bug_id: str,
    bug_title: str,
    fault_time: str,
    codex_summary: str,
    claude_summary: str,
    omlx_summary: str,
    codex_path: Path,
    claude_path: Path,
    omlx_path: Path,
) -> str:
    codex = classify_provider(codex_summary)
    claude = classify_provider(claude_summary)
    omlx = classify_provider(omlx_summary)

    cards = [
        ("最终采信", "Codex", "green", "这份结论最完整，也最明确保留了证据边界"),
        ("共同异常点", "LD Normal / 132002", "red", "三份结果都把焦点放在 LD 相关链路缺失"),
        ("主链路判断", "2 / 3 一致正常", "yellow", "Codex 与 Claude 都重建出 41001/41005 -> 132000/132001 正常"),
        ("源码参与度", "仅 Codex 明确引用", "yellow", "Codex 明确引用 signal.proto；Claude 与 oMLX 输出未体现源码证据"),
        ("问题时间", fault_time, "green", "三份对比都围绕同一故障时间"),
        ("比较对象", "Codex / Claude / oMLX", "green", f"Bug {bug_id}"),
    ]

    issues = [
        {
            "sev": "green",
            "title": "Codex 结果最稳",
            "detail": "同时结合结构化产物、原始日志和源码定义，明确指出主整包链路正常，但不把断点强定到单层。",
        },
        {
            "sev": "yellow",
            "title": "Claude 与 Codex 方向一致，但归因更激进",
            "detail": "Claude 直接写成“百度 LD Normal 数据完全缺失，是直接原因”，证据强度高于当前材料实际能支持的边界。",
        },
        {
            "sev": "yellow",
            "title": "oMLX 结果偏弱",
            "detail": "它主要停留在脚本缺项层，没把主链路正常性和源码语义完整重建出来，更适合作为 quick sanity check。",
        },
        {
            "sev": "red",
            "title": "三份结果都还没把断点压到单一层",
            "detail": "当前仍不能把问题精确压到“百度 LD 源、MapDataHandler、native 组包、Unity 消费”中的某一层。",
        },
    ]

    chain = [
        {
            "sev": "green",
            "title": "对齐同一输入边界",
            "evidence": "三方都围绕同一 bug、同一问题时间、同一已有结构化材料；Codex/Claude 还能按 prompt 读本地文件。",
            "downstream": "对比的是结论质量差异，而不是不同 case 的偶然差异。",
        },
        {
            "sev": "green",
            "title": "主整包链路是否全断",
            "evidence": "Codex 与 Claude 都重建出 41001/41005、132000/132001 的持续流动；oMLX 没重建到这一层。",
            "downstream": "“SR 无感知显示”不宜直接写成整条感知链全断。",
        },
        {
            "sev": "red",
            "title": "LD Normal 分支是共同异常焦点",
            "evidence": "三份结果都提到 LD Normal / MapDataHandler / 132002 缺失或未命中，是唯一稳定收敛的异常点。",
            "downstream": "后续排查应优先围绕 132002 分支继续收敛，而不是回头泛扫全部感知链。",
        },
        {
            "sev": "yellow",
            "title": "根因边界出现分歧",
            "evidence": "Codex 明确保留“不能强定单点根因”；Claude 倾向把缺失直接写成直接原因；oMLX 则只敢说当前无法判断。",
            "downstream": "当前最可信的产物应优先采纳 Codex 版，再把 Claude 作为侧证、oMLX 作为保守参考。",
        },
    ]

    compare_rows = [
        (
            "输入能力",
            "本地文件 + 结构化产物 + 源码目录",
            "本地文件读取工具",
            "内嵌材料，无文件工具",
            "oMLX 对比天然较弱，不是完全同能力对比",
        ),
        (
            "主链路判断",
            codex["main_chain_ok"],
            claude["main_chain_ok"],
            omlx["main_chain_ok"],
            "Codex/Claude 都明确主整包链路未全断",
        ),
        (
            "LD 分支异常",
            codex["ld_focus"],
            claude["ld_focus"],
            omlx["ld_focus"],
            "三方都把异常焦点收敛到 LD Normal / MapDataHandler / 132002",
        ),
        (
            "源码证据",
            codex["source_used"],
            claude["source_used"],
            omlx["source_used"],
            "只有 Codex 最终输出明确引用 signal.proto",
        ),
        (
            "归因风格",
            codex["stance"],
            claude["stance"],
            omlx["stance"],
            "Codex 平衡、Claude 激进、oMLX 保守",
        ),
        (
            "根因表述强度",
            codex["root_cause_strength"],
            claude["root_cause_strength"],
            omlx["root_cause_strength"],
            "Claude 强于证据边界，oMLX 弱于现有材料价值",
        ),
        (
            "建议动作质量",
            "最具体",
            "较具体",
            "偏泛化",
            "Codex 建议最接近下一轮真实可执行排查路径",
        ),
    ]

    alignment_rows = [
        ("Codex", "主整包链路正常，LD 分支异常，不能强定单点根因"),
        ("Claude", "主整包链路正常，LD 分支异常，但更倾向直接定为直接原因"),
        ("oMLX", "确认 LD 统计缺失，但无法判断主链路是否正常"),
    ]

    body = [
        '<div class="container">',
        '<div class="session-header">',
        '<div>',
        f"<h1>三份 Agent 结果对比汇总</h1>",
        (
            '<div class="sub">'
            f"面向 Bug <code>{module.H(bug_id)}</code>《{module.H(bug_title)}》的三方结果汇总。"
            f"对比对象为当前桥里产出的 <code>Codex</code>、本机复跑的 <code>Claude</code>、"
            f"以及基于同批材料内嵌后生成的 <code>oMLX</code> 结果。"
            "本页复用 3d-stuck-investigate 风格：顶部一句话结论、总览卡片、红黄绿状态色、异常摘要、"
            "链路式结论与可折叠证据区。"
            "</div>"
        ),
        '</div>',
        '<div class="session-meta">',
        f"<div><b>Bug ID：</b>{module.H(bug_id)}</div>",
        f"<div><b>问题时间：</b>{module.H(fault_time)}</div>",
        '<div><b>汇总结论：</b>当前最可信的是 Codex 版；Claude 可作侧证，oMLX 仅作保守参考。</div>',
        '</div>',
        '</div>',
        '<div class="verdict v-yellow">结论：三份结果共同收敛到 <code>LD Normal / 132002</code> 分支异常，但只有 Codex 同时把“主整包链路正常”“源码语义”“证据边界”三件事都交代完整，因此当前应以 Codex 版作为主结论。</div>',
        f'<div class="cards">{module.render_cards(cards)}</div>',
        '<div class="section"><h2>异常摘要</h2>',
        module.render_issue_list(issues),
        '</div>',
        module.render_chain(
            chain,
            title="根因判断差异链",
            description='按"输入能力 → 主链路判断 → 共同异常焦点 → 根因边界分歧"组织三方差异。',
        ),
        '<div class="section"><h2>三方对比矩阵</h2>',
        module.render_table(
            compare_rows,
            ["维度", "Codex", "Claude", "oMLX", "汇总判断"],
        ),
        '</div>',
        '<div class="section"><h2>结论对齐</h2>',
        module.render_table(
            alignment_rows,
            ["提供方", "一句话立场"],
        ),
        '</div>',
        '<div class="section"><h2>产物路径</h2>',
        module.render_table(
            [
                ("Codex", str(codex_path)),
                ("Claude", str(claude_path)),
                ("oMLX", str(omlx_path)),
            ],
            ["提供方", "摘要文件"],
        ),
        '</div>',
        '<div class="section"><h2>可折叠证据区</h2>',
        '<details open><summary>Codex 摘要节选</summary><pre>',
        module.H(summary_excerpt(codex_summary, 2400)),
        '</pre></details>',
        '<details><summary>Claude 摘要节选</summary><pre>',
        module.H(summary_excerpt(claude_summary, 2200)),
        '</pre></details>',
        '<details><summary>oMLX 摘要节选</summary><pre>',
        module.H(summary_excerpt(omlx_summary, 1800)),
        '</pre></details>',
        '<details><summary>三方首批关键 bullet</summary><pre>',
        module.H(
            "Codex:\n- "
            + "\n- ".join(extract_bullets(codex_summary) or ["(无)"])
            + "\n\nClaude:\n- "
            + "\n- ".join(extract_bullets(claude_summary) or ["(无)"])
            + "\n\noMLX:\n- "
            + "\n- ".join(extract_bullets(omlx_summary) or ["(无)"])
        ),
        '</pre></details>',
        '</div>',
        '</div>',
    ]
    return module.render_document(
        title=f"Bug {bug_id} Agent 对比汇总",
        body_html="".join(body),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bug-id", required=True)
    parser.add_argument("--bug-title", required=True)
    parser.add_argument("--fault-time", required=True)
    parser.add_argument("--codex-summary", required=True, type=Path)
    parser.add_argument("--claude-summary", required=True, type=Path)
    parser.add_argument("--omlx-summary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    module = load_report_html_module(SHARED_REPORT_HTML)
    html_text = build_report(
        module=module,
        bug_id=args.bug_id,
        bug_title=args.bug_title,
        fault_time=args.fault_time,
        codex_summary=read_text(args.codex_summary),
        claude_summary=read_text(args.claude_summary),
        omlx_summary=read_text(args.omlx_summary),
        codex_path=args.codex_summary,
        claude_path=args.claude_summary,
        omlx_path=args.omlx_summary,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
