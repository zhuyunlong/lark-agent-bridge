# Skill-Selection Reasoning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 取代 `2026-05-30-agent-autonomous-bug-analysis.md`(已废弃)。那份方案把问题放在了解码/执行/HTML 层，改动面过大且风险高；真实痛点仍然是 **skill 选择层**：①对措辞敏感，换说法/带错字就会误选；②路由逻辑死板，模型没有足够语义材料做推理。本计划只改 **bug skill 选择与低置信交互**，不动执行与报告链路。

**Goal:** 让 Bug 首轮分类和续聊重分析都从“字面关键词匹配”升级为“基于 skill 元数据的语义推理”，并把低置信结果通过现有澄清/Skill 按钮链路暴露给用户。

**Architecture:** 用 `skill_registry.py` 提供兼容旧调用的结构化 frontmatter 解析；`bug_runner.py` 基于该元数据构造完整 skill catalog，并让首轮分类和续聊分类共用同一套 prompt/result helper，避免两套规则漂移。低置信时不新造交互面，而是复用现有 `needs_user_direction + supported_bug_skills` 机制，并补充 `classification_confidence / classification_candidates` 供卡片与调试面展示。

**Tech Stack:** Python 3.13 stdlib；`lark_agent_bridge/skill_registry.py`、`lark_agent_bridge/agents/bug_runner.py`、`lark_agent_bridge/skill_manager.py`、`lark_agent_bridge/app.py`、`config/routing_terms.toml`、`tests/test_skill_manager.py`、`tests/test_agents.py`、`tests/test_app.py`、`pytest`/`unittest`。

---

## Context

Baseline reality（已对照当前代码核实，实施前重新确认行号）：

- Bug 首轮选择已经是“Agent 先分，再本地回退”：
  `BugAnalysisRunner._unified_classify_and_decide()` 先调 `_classify_bug_request_with_agent()`，失败才退 `_manual_bug_selection()`。
- 问题不在“没有用 LLM”，而在于 **prompt 仍然硬编码 literal 规则**：
  `_classify_bug_request_with_agent()` 和 `decide_bug_followup()` 都直接写了 `xtheme/105004/晨曦`、`小憩/露营/洗车`、`signal-chain-analyzer 优先级最低` 这类字面词规则。
- 当前 frontmatter 解析函数不在 `bug_runner.py`，而是在 `lark_agent_bridge/skill_registry.py::extract_skill_frontmatter()`；它现在只返回 `(name, description)`，没有结构化元数据。
- 当前分类结果也没有走 pydantic 结构化输出模型；`_run_bug_decision_agent()` 只是执行子进程，`_parse_bug_decision_json()` 再直接 `json.loads()`。因此只改 `agents/pydantic_models.py` 或 `agent_output_models.py` 不会影响 bug skill 分类。
- `app.py` 当前只消费 `classification_source / classification_reason / classification_provider` 以及 `supported_bug_skills`，并依赖 `needs_user_direction` 来决定是否展示补充方向提示与 Skill 按钮。
- skill 元数据实际读取目录是 `BridgeConfig.workspace_root/.ai/skills`。计划里的 frontmatter 内容工作必须落到 **当前验证配置真正使用的 workspace_root**；不能假定永远是某个固定 guideengine worktree 路径。

Non-goals：

- 不碰日志解码、确定性分析脚本、HTML 生成、agent 权限/沙箱。
- 不删除 `routing_terms.toml`、`classify_requests()`、`_manual_bug_selection()`；它们继续作为 LLM 不可用时的离线兜底。
- 不把“选 skill”升级成“先读日志/源码再选 skill”；选择层仍然只基于用户请求、标题、描述、附件摘要和 skill catalog。
- 不顺手改动 signal lifecycle、感知总结、OMLX、知识库等非 bug skill 选择路径。

---

## Decision Log

- **D1 解析契约保持 stdlib-only：** 新增结构化 frontmatter helper，但不引入 `PyYAML`。frontmatter v1 仅支持单行标量字段和单行 inline list（如 `["a", "b"]`）；不支持多行 YAML block。
- **D2 catalog 必须先完整，再谈推理：** `_available_bug_skills()` 不能依赖目录里“刚好存在所有 built-in skill 的 `SKILL.md`”。它应先从 `PRIMARY_BUG_SKILL_MAP` / `AUX_BUG_SKILLS` 种出完整候选集，再用本地 `SKILL.md` 覆盖说明性元数据。
- **D3 首轮分类与续聊分类必须共用同一套推理 helper：** 否则首轮改成语义推理后，追问/重分析仍会按旧 literal 规则漂移。
- **D4 低置信不新增 UI 控件：** 继续复用现有 Skill 按钮与补充方向提示，只在结果详情中增加 `classification_confidence` 和 `classification_candidates`，并由 `app.py` 把这些信息展示出来。
- **D5 这次不引入 pydantic bug-classifier model：** 当前 bug classifier 不是 `output_type` 路径；先把 prompt、JSON 解析和数据流打通，避免做一半迁移。
- **D6 不额外改 `state.py`：** 当前首轮结果会通过 `TaskResult.details` 进入卡片/UI，续聊重分析读取的是 `previous_session["details"]` 而不是 `ConversationContext` 的完整分类载荷；本计划先不为 `confidence/candidates` 增加长期持久化。

---

## File Map

- 修改 `lark_agent_bridge/skill_registry.py`
  - 新增结构化 frontmatter 解析 helper。
  - 保留现有 `extract_skill_frontmatter()` 作为兼容包装，避免 `SkillManager` 等调用方被一次性打碎。
- 修改 `lark_agent_bridge/agents/bug_runner.py`
  - `_available_bug_skills()`：生成完整 catalog，并附带 `when_to_use / symptoms / not_for / examples`。
  - 新增共享 helper：catalog -> prompt payload、prompt 文本构造、classifier JSON 解析、low-confidence 判定。
  - 同时修改 `_classify_bug_request_with_agent()` 与 `decide_bug_followup()`，彻底删除 prompt 中的硬编码字面规则。
  - 扩展 `BugAnalysisSelection` / `BugFollowupSelection`，补充 `confidence` 与 `candidates`。
  - 在首轮分析结果详情和续聊决策里写入 `classification_confidence / classification_candidates`。
- 修改 `lark_agent_bridge/app.py`
  - 在 Skill 选择提示文案里展示低置信候选与当前置信度。
  - 继续复用现有 Skill 按钮，不新增按钮类型。
- 修改 `config/routing_terms.toml`
  - 仅补充注释，明确这是 LLM 不可用时的本地兜底词表，不再是“主事实源”。
- 修改运行时实际使用的 `workspace_root/.ai/skills/*/SKILL.md`
  - 为 bug 主分析 skill 补充 `when_to_use / symptoms / not_for / examples` 单行 frontmatter 字段。
  - 只修改当前验证配置真正会读取的 skill 根目录。
- 修改/新增测试
  - `tests/test_skill_manager.py`：frontmatter 解析与兼容性。
  - `tests/test_agents.py`：rich catalog、首轮分类、续聊分类、低置信细节。
  - `tests/test_app.py`：低置信提示文案与 Skill 按钮说明。
  - 新增 `tests/data/bug_skill_selection_cases.json`：标注集。
  - 新增 `scripts/eval_bug_skill_selection.py`：改前/改后 live benchmark。

---

## Task 1: 锁定 frontmatter 契约与兼容解析

**Files:**
- Modify: `lark_agent_bridge/skill_registry.py`
- Test: `tests/test_skill_manager.py`

- [ ] **Step 1:** 先写失败测试，覆盖新旧两类输入：
  - 旧格式只含 `name / description` 仍能被 `SkillManager` 正常读取。
  - 新格式增加：
    - `when_to_use: 一句话`
    - `symptoms: ["词1", "词2"]`
    - `not_for: 一句话`
    - `examples: ["例子1", "例子2"]`
  - 断言结构化 helper 能返回完整字段，兼容 wrapper 仍返回 `(name, description)`。
- [ ] **Step 2:** 在 `skill_registry.py` 新增例如 `extract_skill_frontmatter_metadata(text: str) -> dict[str, object]` 的 helper：
  - 只解析 frontmatter 区域。
  - 标量字段返回 `str`。
  - inline list 优先走 `ast.literal_eval`，失败时回退为单个字符串或逗号拆分后的列表。
  - 缺省字段统一补空字符串/空列表，避免下游判空分支过多。
- [ ] **Step 3:** 保留现有 `extract_skill_frontmatter()`，内部改为调用新的 metadata helper，并继续只返回 `(name, description)`，确保 `SkillManager` 与现有测试不被破坏。

## Task 2: 构造完整 rich catalog，而不是依赖本地目录是否齐全

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1:** 先写失败测试，覆盖两个关键场景：
  - `workspace_root/.ai/skills` 目录存在但只含少量 skill 文件时，built-in primary skills 仍然会出现在 `_available_bug_skills()` 输出里。
  - 存在对应 `SKILL.md` 时，catalog entry 会附带 `when_to_use / symptoms / not_for / examples`。
- [ ] **Step 2:** 重写 `_available_bug_skills()` 的组装方式：
  - 先根据 `primary_skill_map()` 种出 primary entries。
  - 再根据 `auxiliary_skill_names()` 种出 auxiliary entries。
  - 最后扫描 `workspace_root/.ai/skills/*/SKILL.md`，用 frontmatter 元数据覆盖已有 entry，或为额外 custom/auxiliary skill 新建 entry。
- [ ] **Step 3:** `general` 继续作为显式 primary fallback 保留，并补默认 `when_to_use / not_for`，避免 model 在 catalog 里把 `general` 当成“万能源码分析”。

## Task 3: 用共享 helper 替换首轮分类与续聊分类里的硬编码关键词 prompt

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1:** 先写失败测试，至少覆盖：
  - 首轮分类 prompt 不再包含 `晨曦`、`小憩`、`露营`、`洗车` 等硬编码示例词。
  - 续聊分类 prompt 也不再包含这些硬编码示例词。
  - 两条路径都改为携带 rich catalog 元数据。
- [ ] **Step 2:** 在 `bug_runner.py` 新增共享 helper，建议最少拆出三层：
  - 构造 classifier payload（首轮与续聊输入差异只体现在 request/followup/summary/log presence 上）。
  - 构造 classifier prompt（共享语义推理规则、字段约束、低置信行为要求）。
  - 解析 classifier response（统一解析 `skill / analysis_kind / reason / confidence / candidates`，续聊额外解析 `action / retry_download_if_missing`）。
- [ ] **Step 3:** 修改 `_classify_bug_request_with_agent()`：
  - 删除 literal routing 规则文本。
  - 改为要求模型“优先依据 `when_to_use / not_for / symptoms / examples` 推理，不要因为单个词面相似就硬匹配”。
  - 输出 JSON 升级为：
    - `analysis_kind`
    - `skill`
    - `signal_hint`
    - `reason`
    - `confidence`
    - `candidates`
- [ ] **Step 4:** 修改 `decide_bug_followup()`，复用同一套 helper：
  - 保留 `action / retry_download_if_missing` 续聊特有字段。
  - 删除另一套重复的 literal rules，防止首轮与追问的路由逻辑分叉。
- [ ] **Step 5:** 保持 `_manual_bug_selection()` 原样可用，仅在 agent JSON 解析失败、进程失败、或关键字段缺失时回退。

## Task 4: 把 confidence / candidates 走完整条结果链路

**Files:**
- Modify: `lark_agent_bridge/agents/bug_runner.py`
- Modify: `lark_agent_bridge/app.py`
- Test: `tests/test_agents.py`
- Test: `tests/test_app.py`

- [ ] **Step 1:** 扩展 `BugAnalysisSelection` 与 `BugFollowupSelection` dataclass：
  - 增加 `confidence: float = 0.0`
  - 增加 `candidates: list[dict[str, str]] = field(default_factory=list)`
- [ ] **Step 2:** 在 classifier 结果转 selection 时写入：
  - `selection.confidence`
  - `selection.candidates`
  - 并对非法值做钳制：`confidence` 非法时按 `0.0` 处理。
- [ ] **Step 3:** 在首轮 bug 分析里补 low-confidence 处理规则：
  - 如果命中 `general` 且 `confidence` 低，继续走现有 `_general_direction_needed_result()` 风格的澄清分支。
  - 如果命中了专用 skill 但 `confidence` 低，不直接新建交互，而是在结果详情里写入：
    - `classification_confidence`
    - `classification_candidates`
    - `needs_user_direction = True`
    - `supported_bug_skills`
    让现有 Skill 按钮继续承担“人工纠偏”的入口。
- [ ] **Step 4:** 在正常报告结果、直传分析结果、续聊重分析结果里也一并记录 `classification_confidence / classification_candidates`，确保：
  - `agent_activity.json`
  - 报告元数据
  - report server 调试页
  都能看到这一层决策信息。
- [ ] **Step 5:** 更新 `app.py` 的 Skill 选择提示文案：
  - 如果存在 `classification_candidates`，在 note 中展示“低置信候选：A / B”。
  - 如果存在 `classification_confidence`，展示当前置信度数值或分档。
  - 不新增按钮类型，继续复用现有 Skill 纠偏按钮。

## Task 5: 在实际运行时 skill 根目录补齐 frontmatter 元数据

**Files:**
- Modify: `ACTIVE_WORKSPACE_ROOT/.ai/skills/*/SKILL.md`

- [ ] **Step 1:** 在执行此任务前先锁定“你实际要验证的 bridge profile 对应哪个 `BridgeConfig.workspace_root`”。
  - 如果当前只在本仓库本地验证，skill 根目录就是 `lark-agent-bridge/.ai/skills`。
  - 如果你验证的是 bridge 指向 guideengine worktree 的真实运行配置，就必须编辑那个 worktree 下的 `.ai/skills`，而不是只改 bridge 仓库自己的 `.ai/skills`。
- [ ] **Step 2:** 至少为以下 primary bug skill 补齐单行 frontmatter 字段：
  - `signal-chain-analyzer`
  - `scene-signal-diagnosis`
  - `3d-stuck-investigate`
  - `unity-startup-lifecycle-check`
  - `xtheme-analyzer`
  - `perception-data-summary`
  - `ld-lane-level-log-analysis-portable`
  - `pullover-chain-analyzer`
- [ ] **Step 3:** frontmatter 内容规则：
  - `when_to_use` 写“什么症状/诉求时该用”
  - `symptoms` 写典型现象关键词列表
  - `not_for` 明确与其它 skill 的互斥边界
  - `examples` 写 1-2 条高代表性的自然语言问法
- [ ] **Step 4:** 对 `signal-chain-analyzer` 单独强调边界：
  - 只在“用户明确要排查具体 SignalCode / SIGNAL_* 链路、来源、送达情况”时使用。
  - `not_for` 明确排除“泛泛说到信号”“场景模式异常但未锁定具体 signal code”的场景。
- [ ] **Step 5:** 用与运行配置一致的 `workspace_root` 跑一次 `SkillManager.debug_skill()`，确认这些字段可读、不会因为 parser 限制丢失。

## Task 6: 做一份可重复运行的标注集与 live benchmark

**Files:**
- Create: `tests/data/bug_skill_selection_cases.json`
- Create: `scripts/eval_bug_skill_selection.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1:** 先整理标注集，每条至少包含：
  - `prompt_text`
  - `title`
  - `description`
  - `expected_skill`
  - `bucket`（如 `paraphrase` / `typo` / `scene-vs-signal` / `xtheme` / `general`）
- [ ] **Step 2:** 把“最容易被 literal 规则误导”的样本放进去：
  - XTheme 的换说法，不直接出现 `xtheme/晨曦`
  - 场景模式异常，但不直接出现 `小憩/露营/洗车`
  - 泛化“信号异常”与“具体 SignalCode 链路”区分样本
  - 模糊请求应走 `general + clarification` 的样本
- [ ] **Step 3:** 新增 `scripts/eval_bug_skill_selection.py`：
  - 读取标注集。
  - 调用当前分支上的 classifier 路径。
  - 输出总准确率、各 bucket 准确率、低置信触发率、fallback 触发率。
- [ ] **Step 4:** 在替换 prompt 前先跑一次 baseline，把结果落到 `docs/superpowers/` 或同级 artifact 目录；改完后再跑一次，明确比较：
  - 总准确率不能低于 baseline。
  - `paraphrase / typo` 子集必须提升。
  - `scene-vs-signal` 与 `signal-chain` 不得明显回归。
- [ ] **Step 5:** 依据 live benchmark 结果回填 low-confidence threshold，避免拍脑袋设置。

---

## Verification

先跑单测，再跑 live benchmark，最后做一次真实会话 smoke test。

```bash
cd /Users/zhuyl/Documents/workspace/tools/lark-agent-bridge

PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_skill_manager.py -k "frontmatter or debug"
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_agents.py -k "classify_bug_request_with_agent or decide_bug_followup or available_bug_skills or confidence"
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_app.py -k "skill_choice or direction"

PYTHONPATH=. .venv/bin/python scripts/eval_bug_skill_selection.py --cases tests/data/bug_skill_selection_cases.json

git diff --check
```

真实会话 smoke test：

1. 用 XTheme 换说法样本（不显式写 `xtheme/晨曦`）发起 bug 分析，确认仍选 `xtheme-analyzer`。
2. 用“场景模式不对/模式切换异常”但不含固定词面的小样本发起分析，确认优先落到 `scene-signal-diagnosis`，而不是因为“信号”二字误选 `signal-chain-analyzer`。
3. 用模糊请求发起分析，确认结果会带 `needs_user_direction=true`、Skill 按钮可见，并且详情里能看到 `classification_candidates`。
4. 人工让 classifier 不可用（错误 command 或 provider），确认 `_manual_bug_selection()` 仍可兜底，不阻塞分析。
5. 看 `data/state/agent_activity.json`、报告元数据和 report server 调试页，确认能看到 `classification_confidence` 与 `classification_candidates`。

---

## Risks

- **R1 catalog 仍然不完整：** 如果实际运行 profile 的 `workspace_root` 下没有对应 `SKILL.md`，模型只能拿到默认 label/description。Task 2 通过“先种 built-ins、再 overlay metadata”缓解，但 Task 5 仍要补齐真正运行时的 skill 文件。
- **R2 首轮/续聊漂移：** 若只改 `_classify_bug_request_with_agent()` 而漏改 `decide_bug_followup()`，追问重分析仍会走旧规则。Task 3 必须把两条路径一起收敛。
- **R3 low-confidence 太激进：** 阈值过高会导致大量不必要澄清；过低又会回到“自信硬选”。必须以 Task 6 的标注集校准。
- **R4 parser 过于“像 YAML 但不是 YAML”：** 如果 frontmatter 作者写多行 block/list，stdlib parser 可能读不出来。需要在文档模板或 code review 中明确“仅支持单行字段/inline list”。
