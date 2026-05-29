# 源码分析运行时对比报告

更新时间：2026-05-29

## 最终结论（2026-05-29 05:20）

- 旧结论“`codex app-server` 还没有在真实 bridge 路径上跑通”已经过时。
- 当前最新真实群聊续聊样本：`om_x100b6ebf4378a0a0c3d59a65389b10a`
  - `analysis_kinds = ["scene_signal", "source_stage"]`
  - `analysis_skill = scene-signal-diagnosis`
  - `source_stage_executor = codex_app_server`
  - `source_stage_agent_duration_seconds = 175.87`
  - `agent_summary_execution_backend = source_stage_direct`
  - `total duration_seconds = 181.59`
- 这条样本已经满足“必须跑出源码分析结果，并优化到 5 分钟内”的目标。

### 让真实 bridge 样本压进 5 分钟的关键修复

1. 显式源码追问不再沿用污染过的 `xtheme` 上下文；续聊决策改为识别 `scene-signal-diagnosis`。
2. `source_stage` 的 file-agent `request_text` 改成只保留当前源码追问，旧上下文只放到精简 reference/description。
3. 飞书进度卡对 `*_stream` 阶段做节流，避免每条 `Codex delta` 都触发一次 `update_card` 网络往返。
4. 保留 `codex app-server` 的最小 runtime 面：`preserve_proxy_env=true`、最小 `CODEX_HOME`、关闭 `apps/plugins/computer_use/memories`、限制源码根 cwd。

## 结论摘要

- `pydantic_ai` 能完成这类 3D 场景源码分析，但本次真实群聊样本总耗时约 `333.0s`，已经超过“5 分钟算失败”的阈值。
- `codex exec file-agent` 在真实群聊续聊链里失败更早：`source_stage_reanalysis_agent_timeout`，耗时约 `183.5s`，没有产出可验证正文。
- `codex app-server` 已经真实接入 bridge，并在群聊里刷出了流式进度节点；但本次样本在 `181.8s` 左右触发 `codex_app_server_turn_timeout`，随后 bridge 回退到既有 `codex exec` 路径。
- 从当前样本看，`codex app-server` 的价值主要体现在“可见的流式阶段节点和事件审计”，还没有在这条复杂源码分析链上跑出比 `codex exec` 更稳的完成结果。

## 测试范围

- 群聊：`oc_d977fe30a92c7ac81e3e6b543d99ef5b`
- 目标 Bug：`https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703`
- 代表消息：
  - 顶层消息：`@朱云龙的飞书 CLI 问题时间 2025-05-10 14:30:00 源码分析 3D场景信号分析 https://project.feishu.cn/xpfailuremgmt/buglo/detail/6998811703`
  - 续聊消息：`@朱云龙的飞书 CLI 基于源码重新分析，检查信号处理相关的代码逻辑`
- 说明：
  - `pydantic_ai` 样本使用顶层消息。
  - `codex exec file-agent` 与 `codex app-server` 样本使用同一条已完成卡片的续聊链，复用缓存日志和 bug 上下文。
  - 为强制进入 file-agent 样本，临时配置关闭了 `ai_provider` 的模型面，并把 `scene-signal-diagnosis` 路由到 `source_code_skill`。

## 样本对比

| 模式 | 真实群聊样本 | bridge 观测 | 关键指标 | 5 分钟判定 | 结果 |
|---|---|---|---|---|---|
| `pydantic_ai` | 顶层消息 `om_x100b6eb3e3d4eca0c4fc4bf6c6ed6a9` | `analysis_kind=source_code_skill`，`analysis_skill=scene-signal-diagnosis`，`source_code_skill_agent_provider=pydantic_ai` | 源码分析 `282.38s`；tool calls `34`；总结 `50.63s`；总结 tokens `21068`；总耗时约 `333.01s` | 超过 `300s`，按规则记失败 | 业务结果成功，但 benchmark 规则下失败 |
| `codex exec file-agent` | 续聊消息 `om_x100b6eb3b37438a0c23653df652d8a0` | `mode=bug_reanalysis`，`analysis_skill=source_analysis`，`provider=codex` | 失败码 `source_stage_reanalysis_agent_timeout`；bridge stdout 中记录耗时 `183.50s`；stderr 末尾仅有 `Reading additional input from stdin...` | 未超过 `300s`，但阶段内超时失败 | 失败 |
| `codex app-server` | 续聊消息 `om_x100b6ebc407918a0c385183239d0d61` | 真实刷出 `source_stage_agent_analysis_stream`；事件落盘到 `source_stage.app_server_events.jsonl` | app-server 启动线程 `019e6fb7-bb85-7093-8c31-66b8fc83c03e`；turn `019e6fb7-bdb6-7e23-bd17-2132ad850b21`；约 `181.82s` 后 `app_server_error_code=codex_app_server_turn_timeout`；随后 fallback 到 `codex exec` | app-server 本段未在 5 分钟内形成最终成功结果 | 失败（但流式接入成立） |

## 关键证据

### 1. `pydantic_ai`：能完成，但超 5 分钟阈值

- 会话：`om_x100b6eb3e3d4eca0c4fc4bf6c6ed6a9`
- 证据：
  - `data/state/agent_activity.json` 中该 session 的 `details.source_code_skill_agent_duration_seconds = 282.3826334159821`
  - `details.agent_summary_duration_seconds = 50.63195608416572`
  - `details.agent_summary_total_tokens = 21068`
  - `progress` 中有 `source_code_skill_pydantic_ai_done`，其 `tool_calls = 34`
- 计算：
  - `282.3826 + 50.6320 = 333.0146s`
  - 已超过用户要求的 `300s` 阈值
- 报告产物：
  - HTML：`data/jobs/44cb3e36d148afda2c82e7f97c467f62/output/bug_source_code_report.html`
  - 发布页：`http://192.168.71.15:8765/reports/44cb3e36d148afda2c82e7f97c467f62/v42/`

### 2. `codex exec file-agent`：超时失败

- 续聊消息：`om_x100b6eb3b37438a0c23653df652d8a0`
- 证据：
  - `data/state/agent_activity.json` 中 `status=failed`
  - `error_code=source_stage_reanalysis_agent_timeout`
  - `details.analysis_skill=source_analysis`
  - `details.command_path=/Users/zhuyl/Documents/workspace/tools/lark-agent-bridge/data/jobs/12e257a7578da4b77fc0452b685459b5/output/source_stage_analysis/source_stage.command.txt`
  - bridge 运行日志给出 `duration_seconds = 183.4970251249615`
  - `stderr` 只留下 `Reading additional input from stdin...`
- 结论：
  - 这条链在 file-agent 正文阶段就超时，没能进入有效总结。

### 3. `codex app-server`：流式节点生效，但 turn 超时后回退

- 续聊消息：`om_x100b6ebc407918a0c385183239d0d61`
- 证据：
  - `data/state/agent_activity.json` 的 root session `om_x100b6eb293c4d8a8c101837547b37cb` 里，连续出现：
    - `source_stage_agent_analysis_stream: Codex thread started`
    - `source_stage_agent_analysis_stream: MCP node_repl ready`
    - `source_stage_agent_analysis_stream: Codex turn started`
    - `source_stage_agent_analysis_stream: MCP codex_apps failed`
    - `source_stage_agent_analysis_stream: Codex app-server 失败，回退到现有 codex exec 路径`
  - 同一条 progress 的 `details.app_server_error_code = codex_app_server_turn_timeout`
  - 事件审计文件：
    - `data/jobs/12e257a7578da4b77fc0452b685459b5/output/source_stage_analysis/source_stage.app_server_events.jsonl`
  - app-server 命令文件：
    - `data/jobs/12e257a7578da4b77fc0452b685459b5/output/source_stage_analysis/source_stage.command.txt`
  - 事件流中可见：
    - thread id `019e6fb7-bb85-7093-8c31-66b8fc83c03e`
    - turn id `019e6fb7-bdb6-7e23-bd17-2132ad850b21`
  - 起止时间（来自 progress 时间戳）：
    - `source_stage_agent_analysis`：`2026-05-28T17:52:52.042580+00:00`
    - `Codex app-server 失败，回退到现有 codex exec 路径`：`2026-05-28T17:55:53.863395+00:00`
    - 约 `181.82s`
- stderr 里还出现了对稳定性的直接信号：
  - `stream disconnected - retrying sampling request (1/5 ... 5/5)`
  - `falling back to HTTP`
  - `codex_apps` MCP 启动失败

## 对 bridge 改造的直接判断

### 已验证成立

- `codex app-server` 已经真实跑进 `lark-agent-bridge`，不是只在单测里打桩。
- bridge 会把 app-server 的实时事件投影成飞书进度节点。
- app-server 事件审计 JSONL 已经落盘，可用于复盘 turn 内部状态。
- app-server 失败后，bridge 会自动 fallback 到现有 `codex exec` 路径。

### 仍未证明更优

- 这条复杂源码分析链上，app-server 还没有跑出比 `codex exec` 更短或更稳的完成结果。
- 当前 app-server 样本在 turn 内部遭遇了网络/stream 重连和 `codex_apps` MCP 启动失败，最终没有在可接受时限内给出正文。
- file-agent 和 app-server 都还没解决 `source_analysis` 在复杂追问里容易卡住的问题；只是 app-server 至少把“卡在哪”暴露出来了。

## 追加隔离结论（本轮）

### 1. 正确的 cwd 与更轻的 runtime 现在已经进代码

- `codex` 文件 agent 的工作目录已从通用 `workspace_root` 收窄到源码根（优先 `source_investigation.repo_roots[0]`）。
- `codex app-server` 路径现在会默认带这些抑制项：
  - `--disable apps`
  - `--disable plugins`
  - `--disable computer_use`
  - `--disable memories`
  - `-c analytics.enabled=false`
  - `-c model_reasoning_effort="medium"`（本地 probe 里也试过 `low`）
- 新增了可选 `use_minimal_home=true`，bridge 会在 `data/codex_app_server_home` 下准备一个最小 `CODEX_HOME`，只复用认证和模型缓存，不再继承桌面态 hooks / plugins / state。
- 新增了 `preserve_proxy_env=true`，只对 app-server 子进程回灌当前 shell 的 `HTTP_PROXY/HTTPS_PROXY/...`；这一项是本轮让 app-server 从“连 `hi` 都跑不出来”变成“能稳定产出源码分析正文”的关键修复。

### 2. app-server 已经真实跑出源码分析正文

- 产物：
  - `data/jobs/12e257a7578da4b77fc0452b685459b5/output/appserver_probe4/source_stage_analysis.md`
  - `data/jobs/12e257a7578da4b77fc0452b685459b5/output/appserver_probe4.json`
- 关键指标：
  - executor: `codex_app_server`
  - provider: `codex`
  - duration: `171.307s`
  - evidence_count: `43`
  - totalTokens: `834612`
  - thread id: `019e7012-5002-7af1-8f05-2e41d990fbd2`
  - turn id: `019e7012-5049-78e0-a31f-4c13b8fd350a`
- 这条样本已经证明：在 bridge 的真实 `source_stage` prompt 上，app-server 不只是“能起线程和刷进度”，而是能产出完整 `## 结论摘要 / ## 关键证据 / ...` Markdown 正文。

### 3. app-server 的阻塞已经被收窄到“采样流本身”

在最小 `CODEX_HOME` + 关闭 `apps/plugins/computer_use/memories` 的前提下，最小 prompt `只回复 hi` 的本地 probe 仍然失败：

- 启动事件只剩：
  - `thread/started`
  - `turn/started`
  - 一次 `item/started` / `item/completed`
- 随后进入固定模式：
  - `Reconnecting... 2/5`
  - `Reconnecting... 3/5`
  - `Reconnecting... 4/5`
  - `Reconnecting... 5/5`
  - `Falling back from WebSockets to HTTPS transport`
  - 最终 `turn_timeout`
- 关键点：
  - 这说明 **不是 source_analysis prompt 太重**，因为 `hi` 级别的最小采样也会在同一位置断流。
  - 这也说明 **不是 bridge 把 app-server 带到 `/` 扫全盘**，因为最小 prompt 不涉及任何源码扫描仍然复现。

### 4. 新的时间画像

- 纯 `source_stage` app-server 已压到 `171s`，已经在 5 分钟内。
- 旧的完整 reanalysis 之所以会到 `585.6s`，主因不再是 app-server，而是后面的 summary 还走了重型 `codex exec`。
- 本轮又继续做了两条优化：
  - 启用 app-server 时，`source_stage` 直接跳过 `pydantic_ai`，不再先走一遍快路径再 fallback。
  - 对“明确要求源码重分析”的续聊，若 `source_stage` 已由 app-server 产出完整 Markdown，可直接把这份正文作为最终回复，避免再跑一轮独立 summary。

### 5. 本轮最有价值的诊断结论

- `codex app-server` 的主 blocker 已经从“完全跑不起来”缩到两类：
  - 无代理时，sampling transport 自身断流；
  - 有代理且 feature/环境收敛后，可以稳定跑出 `source_stage` 正文，剩余瓶颈转移到 summary 选路。
- bridge 现在能：
  - 真实启动 app-server
  - 记录 event audit JSONL
  - 把 `error` / `warning` / fallback 节点投影到 progress
  - 在 app-server 失败后继续回退到 `codex exec`
- 经过代理环境回灌后，“连 `hi` 都出不来”的问题已经缓解；当前最值得继续压缩的是完整续聊链的总耗时，而不是 app-server transport 本身。

## 建议动作

1. 针对 `codex app-server` 单独加一个更激进的 warmup/裁剪策略：
   - 这条已经做了第一轮：关掉 `apps/plugins/computer_use/memories`，并引入最小 `CODEX_HOME`。
   - 下一轮要么继续找 Codex transport 级别开关，要么在同机别的 Codex 环境里做对照，确认是不是宿主环境特有。
2. 对 `source_analysis` prompt 再减上下文体积，尤其是续聊链里 `reference_text -> description -> context.md` 的污染；这条已经做了第一轮裁剪，但还可以继续缩。
3. 把 app-server 的 `error` / `warning` 事件也并入 `source_stage.app_server_events.jsonl` 的摘要统计，别只看 preview 文案。
4. 对 `codex exec file-agent` 的 stdin 使用方式做专项排查：
   - 本次 stderr 只有 `Reading additional input from stdin...`，说明它可能在等待更多输入或没有顺利收束到最终正文。
5. 后续 benchmark 建议固定只跑“续聊重分析”链，不再混顶层新消息：
   - 顶层消息包含 bug 拉取、分类、缓存命中等前置噪音，不利于单独比较源码分析运行时。
