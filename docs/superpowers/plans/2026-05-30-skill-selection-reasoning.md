# Skill-Selection Reasoning Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 取代 `2026-05-30-agent-autonomous-bug-analysis.md`(已废弃)。那份"agent 全包解码+选 skill+分析+出 HTML"的全替换方案,针对的是错误的层、且引入解码跨平台/沙箱写权限/稳定性回退等高风险。真实痛点是 **skill 选择层**:①选择对"措辞"敏感,换说法/错字就选错;②路由死板、不会推理。本计划只改选择层。

**Goal:** 让 Bug skill 选择从"关键词字面匹配"变成"基于每个 skill 的 `when_to_use` 语义推理",消除措辞敏感(痛点①)与规则死板(痛点②)。**不动解码、确定性执行、HTML 生成、agent 权限**——稳定性原样保留。改动局限在一个已存在的分类函数 + skill frontmatter,成本/延迟与现状持平,且用标注集量化验收。

**Architecture:** 选择层就地改造,执行/解码/报告链路不变。
- `_available_bug_skills`(bug_runner.py:549)产出**富 catalog**:每个 skill 除 `description` 外带 `when_to_use / symptoms / not_for / examples`,来源是各 `SKILL.md` frontmatter(单一事实源,加新 skill 不改桥代码)。
- `_classify_bug_request_with_agent`(bug_runner.py:1200)**删掉硬编码"出现X选Y"关键词规则**,改成通用推理指令:依据每个 skill 的 `when_to_use` 与用户症状推理,不按字面词匹配;输出 `{skill, confidence, candidates[], reason}`。
- **低置信不硬选**:返回 top-N 候选给用户确认或要求补方向(复用现有交互模式)。
- `routing_terms.toml` + `_manual_bug_selection`(bug_runner.py:651)**降级为先验/兜底**(LLM 不可用时仍能路由),不再是事实源。

**Tech Stack:** Python 3.13 stdlib;现有 `BugAnalysisRunner` classify 路径(`_unified_classify_and_decide` @832 / `classify_and_decide` @896 / `_classify_bug_request_with_agent` @1200 / `_run_bug_decision_agent` / `_resolve_bug_plans` / `_selection_from_plans`)、`skill_manager.py`、`config/routing_terms.toml`、agent 输出模型(`agents/pydantic_models.py` / `agents/agent_output_models.py`);`pytest`/`unittest`。

---

## Context

Baseline reality(已对照当前代码核实,编辑前重新确认行号):

- skill 选择**已经是一次 LLM 调用**:`_unified_classify_and_decide`(bug_runner.py:832)先调 `_classify_bug_request_with_agent`(:1200),失败才退 `_manual_bug_selection`(:651,纯关键词)。所以问题不是"不够 agentic"。
- 根因在 `_classify_bug_request_with_agent` 的 prompt **塞满硬编码关键词规则**,例如「出现 xtheme/105004/晨曦/主题切换 → xtheme-analyzer」「3D场景信号/小憩/露营/洗车 → scene-signal-diagnosis」「signal-chain-analyzer 优先级最低」。等于把 `routing_terms.toml` 的脆弱关键词搬进提示词,逼模型做字面匹配 → 痛点①。
- catalog 太薄:`_available_bug_skills`(bug_runner.py:549)只从 `SKILL.md` frontmatter 取 `frontmatter_name, description`(`_extract_skill_frontmatter`),每个 skill 只给模型一句话 → 模型缺推理材料 → 痛点②。
- 分类器现输出 JSON `{analysis_kind, skill, signal_hint, reason}`,**无 confidence/candidates**,低置信也会硬选一个。
- `SkillRecord`(skill_manager.py:62 `to_dict`)已有 `description` 字段,`debug_skill` 检查项明确写"建议补充 description 以提高路由可解释性",方向与本计划一致。
- `routing_terms.toml` 现为关键词 tuple(`startup/stuck/signal/scene_signal/perception/crash/xtheme/...`),substring 精确匹配,对 paraphrase/typo 脆弱。

Non-goals:

- 不碰解码(log-decoder)、确定性脚本执行(`build_command`)、HTML 生成、agent 沙箱/权限。
- 不删 `routing_terms.toml` / `_manual_bug_selection`:保留为离线兜底。
- 不引入"让 agent 读日志来决定 skill":标题+描述+富 catalog 足够推理,读日志选 skill 又慢又贵。
- 不改其它路径(signal_lifecycle / 感知总结 / omlx / 知识库)。

---

## Decision Log

- **D1 富元数据落点:** 放在各 `SKILL.md` frontmatter(单一事实源,skill 作者本就在这维护;加新 skill 零桥码改动)。`_extract_skill_frontmatter` 扩展解析。
- **D2 关键词去留:** 保留为先验/兜底,不删。强约束(如 signal-chain 最低优先级、scene vs signal 区分)从"散落关键词"改表达为 skill 自己的 `not_for` / 优先级元数据,由模型推理消化。
- **D3 低置信策略:** 不硬选。低于阈值 → 给 top-2 候选让用户确认,或要求补充方向(复用现有 `_needs_general_direction` 交互)。阈值用标注集校准。
- **D4 验收:** 必须用历史 case 标注集(请求→正确 skill)做改前/改后选择准确率对比,达标才合入。

---

## File Map

- 修改 `lark_agent_bridge/agents/bug_runner.py`
  - `_extract_skill_frontmatter`:解析 `when_to_use / symptoms / not_for / examples`(缺省空)。
  - `_available_bug_skills`(:549):catalog entry 带出上述新字段。
  - `_classify_bug_request_with_agent`(:1200):删硬编码关键词规则;prompt 改为"按 when_to_use 推理";输出加 `confidence` + `candidates`。
  - 消费 `confidence`:低置信走候选确认/要求补方向分支。
- 修改 `lark_agent_bridge/agents/pydantic_models.py` 或 `agent_output_models.py`:分类输出模型加 `confidence: float` + `candidates: list`。
- 修改 `config/routing_terms.toml`:顶部注释标注"先验/兜底,非事实源";不删条目。
- 修改 skill frontmatter(guideengine 仓库 `xp/guideengine/.worktrees/os6_xpdev/.ai/skills/*/SKILL.md`):为 primary 分析 skill 补 `when_to_use` 等字段。
- 测试:扩展 `tests/test_agents.py`(选择推理/低置信)、`tests/test_skill_manager.py`(frontmatter 解析);新增小标注集回归用例。

---

## Task 1: 富 skill 元数据

让 catalog 给模型足够推理材料(治痛点②的基础)。

**Files:** `bug_runner.py`、`tests/test_skill_manager.py`(或 `test_agents.py`)。

- [ ] **Step 1(先写失败测试):** 一个含 `when_to_use:` / `not_for:` frontmatter 的 SKILL.md fixture,断言 `_extract_skill_frontmatter` 解析出这些字段、`_available_bug_skills` entry 含这些字段。
- [ ] **Step 2:** 扩展 `_extract_skill_frontmatter` 解析新字段(向后兼容:缺省为空)。
- [ ] **Step 3:** `_available_bug_skills` 把新字段加入 primary/auxiliary entry。frontmatter 约定(最小):

```yaml
when_to_use: 一句话——什么症状/场景该用这个 skill
symptoms: [典型现象1, 典型现象2]
not_for: 明确不该用的场景（用于互斥推理）
```

## Task 2: 推理式分类 prompt + 置信输出

去掉字面匹配规则(治痛点①),让模型推理并暴露不确定性。

**Files:** `bug_runner.py`、输出模型文件、`tests/test_agents.py`。

- [ ] **Step 1(先写失败测试):** 断言分类 prompt **不含**硬编码示例词清单(如 "晨曦"、"小憩" 这类被搬进 prompt 的关键词规则);断言输出模型含 `confidence` 与 `candidates`。
- [ ] **Step 2:** 重写 `_classify_bug_request_with_agent` 的 prompt:移除"出现X选Y"规则;改为「依据每个 skill 的 `when_to_use`/`not_for` 与用户症状**推理**最匹配项,不要按字面关键词匹配;给出 confidence(0-1)与至多 2 个候选及理由」。catalog 用 Task 1 的富字段。
- [ ] **Step 3:** 输出模型加 `confidence: float`、`candidates: list[{skill, reason}]`;解析失败/字段缺失时安全降级(视为低置信)。

## Task 3: 低置信处理 + 关键词降级

**Files:** `bug_runner.py`、`config/routing_terms.toml`、`tests/test_agents.py`。

- [ ] **Step 1:** `confidence` 低于阈值 → 不硬选,返回 top-2 候选请用户确认 / 要求补方向(复用 `_needs_general_direction` 路径)。阈值先取保守值,Task 5 校准。
- [ ] **Step 2:** `_manual_bug_selection` / `routing_terms.toml` 仅在 LLM 不可用或显式兜底时生效;`routing_terms.toml` 顶部注释标注语义降级。
- [ ] **Step 3:** 测试:换说法/错字命中正确 skill;模糊请求触发候选确认而非硬选;LLM 不可用时关键词兜底仍工作。

## Task 4: 为 primary 分析 skill 补 `when_to_use`(内容工作)

**Files:** guideengine `.ai/skills/*/SKILL.md` frontmatter。

- [ ] 至少覆盖 primary 分析 skill:`signal-chain-analyzer`(强调"仅明确具体 SignalCode 链路时用"——把原来散落的"最低优先级"规则表达成它自己的 `when_to_use`/`not_for`)、`scene-signal-diagnosis`、`3d-stuck-investigate`、`unity-startup-lifecycle-check`、`xtheme-analyzer`、`perception-data-summary`。
- [ ] 用 `skill debug`(skill_manager `debug_skill`)校验 frontmatter 可读。

## Task 5: 选择准确率回归集 + 改前后对比(量化验收)

**Files:** `tests/data/`(标注集)、`tests/test_agents.py` 或独立脚本。

- [ ] 从历史 case / `data/jobs` 抽一批 (请求文本+标题+描述 → 正确 skill) 标注集,含 paraphrase/错字/组合 样本。
- [ ] 跑改前(关键词规则)vs 改后(推理),报告选择准确率、误选率、低置信触发率。
- [ ] 定阈值:改后准确率不低于改前,且 paraphrase/错字子集明显改善,才合入;据此校准 Task 3 的置信阈值。

---

## Verification

```bash
cd /Users/zhuyl/Documents/workspace/tools/lark-agent-bridge
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_agents.py -k "select or classify or confidence"
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_skill_manager.py
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

真实群聊:

1. 用**换说法/带错字**的 bug 请求(如把"主题切换"说成"白天黑夜切换不对"),确认选中 `xtheme-analyzer` 而非误选。
2. 用模糊请求,确认返回候选确认/要求补方向,而非自信硬选。
3. 断开/禁用 LLM,确认关键词兜底仍能路由。
4. 看 `data/state/agent_activity.json`:selection 带 reason 与 confidence。

## Risks

- **R1 去掉硬规则后强约束回归**(signal-chain 滥用、scene vs signal 混淆):用 Task 5 标注集守住;强约束改表达为 skill 的 `not_for`/优先级元数据,而非散落关键词。
- **R2 富 frontmatter 维护成本:** 单一事实源在 skill 自身,且 `debug_skill` 已鼓励补全;先覆盖 primary skill。
- **R3 置信阈值校准:** 用标注集定,过高→频繁追问,过低→回到硬选;Task 5 调。
- **R4 LLM 不可用:** 关键词兜底保留,不回归。
