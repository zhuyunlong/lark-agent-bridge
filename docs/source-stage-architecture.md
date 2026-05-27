# Source Stage Architecture

## 结论

源码分析在 bug-analysis 中是一个阶段，不是一个可被用户或路由直接命中的特殊 skill。

最终执行链路统一为：

```text
Normalize
-> AnalysisDecision
-> DomainStage
-> Optional SourceStage
-> SummaryStage
```

## 阶段边界

### DomainStage

负责业务域判断和领域分析，例如：

- `scene-signal-diagnosis`
- `xtheme-analyzer`
- `ld-lane-level-log-analysis-portable`

DomainStage 产出领域 verdict、证据摘要、日志聚焦范围，并为后续源码阶段提供上下文。

### SourceStage

只在用户明确要求源码/源代码/debug/类名/函数名/文件名时进入。

SourceStage 必须依附于领域上下文：

- `context_profile`
- 前序领域报告 JSON / verdict 摘要
- 聚焦日志范围
- 用户显式源码目标

它不能重新决定业务域，也不能作为用户可选 skill 暴露。

### SummaryStage

只消费前序 artifact 做最终回答。当前序阶段没有有效 artifact 时必须 fail closed，不能绕过阶段直接读大日志输出根因。

## 命名规则

源码阶段统一使用 stage 语义：

- `source_stage`
- `source_stage_report.html`
- `source_stage_report.json`
- `source_stage_analysis.md`
- `source_stage.context.md`
- `source_stage.debug.log`

错误码也使用 stage 语义：

- `source_stage_missing_context`
- `source_stage_executor_not_ready`
- `source_stage_agent_failed`
- `source_stage_agent_empty_output`
- `source_stage_agent_invalid_evidence`

## 触发规则

普通业务请求默认不跑 SourceStage：

- `分析 3D场景模式`
- `分析主题切换问题`
- `调查 LD 退无图`

只有显式源码诉求才允许追加 SourceStage：

- `基于源码分析 3D场景模式`
- `debug 场景信号链路`
- `分析 SRViolationHandler.kt`
- `检查 SomeClass.someMethod`

环境信息不能单独触发源码阶段：

- `Version`
- `Build`
- `Serial`
- `VIN`
- `ICCID`

## 验证要求

普通请求验证：

```bash
PYTHONPATH=. pytest -q tests/test_agents.py -k "source_stage or stage_plan or source_evidence_terms_ignore_environment_labels"
PYTHONPATH=. pytest -q tests/test_app.py -k "source_stage or 6998811703"
git diff --check
```

真实群聊验证：

1. 在目标群发送带 bot mention 的 bug 链接和普通 prompt：`分析 3D场景模式`。
2. 查看 `data/state/agent_activity.json` 的最新 session。
3. 确认普通请求没有创建或运行 SourceStage。
4. 再发送显式源码请求：`基于源码分析 3D场景模式`。
5. 确认 `source_mode=append`，且 SourceStage context 包含前序领域 verdict。
