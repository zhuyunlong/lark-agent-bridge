# Lark Agent Bridge 初版实现计划

> 目标：实现一个本地飞书 Bot Bridge。客户在飞书群或私聊中 @ Bot，并给出信号 code / 信号枚举名 / 自然语言描述，以及日志下载链接或飞书附件后，本地 Bridge 下载日志、调用 guideengine `signal-chain-analyzer` skill 分析信号生命周期，并把报告结果回发飞书；同时提供安全范围内的基础聊天回复能力。

当前计划的主业务仍是 **信号生命周期调查**，同时补充基础聊天回复；架构按可扩展方式拆分，后续可以继续增加 `/review`、`/startup`、`/log`、`/report` 等 handler。

## 一、当前已完成状态

已完成：

- [x] 创建工程目录：`tools/lark-agent-bridge`
- [x] 创建子目录：`lark_agent_bridge/handlers`、`docs`、`samples`、`tests`、`data`
- [x] 创建最小 Python 包文件：`lark_agent_bridge/__init__.py`
- [x] 创建模块入口：`lark_agent_bridge/__main__.py`
- [x] 创建 `pyproject.toml`
- [x] 创建本文档：`docs/implementation-plan.md`

本轮补齐：

- [x] `README.md`
- [x] `config.example.toml`
- [x] `lark_agent_bridge/cli.py`
- [x] 配置加载、模型、解析、策略、状态去重
- [x] 飞书事件消费、消息详情读取、附件下载、消息回复封装
- [x] HTTP 下载器和飞书附件下载器
- [x] `signal_lifecycle` handler
- [x] 调用 guideengine `signal-chain-analyzer` runner
- [x] 样例事件和单元测试
- [x] dry-run 验证和本地 `lark-cli` 检查

## 二、目标能力边界

### 初版支持

- 飞书消息触发：
  - `@Bot /signal 132002 日志 https://...`
  - `@Bot 调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA，日志见附件`
  - `@Bot 帮我看 LD normal 信号有没有到 Unity，日志链接 ...`
- 输入识别：
  - 数字 signal code，例如 `132002`
  - 枚举名，例如 `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA`
  - 简单自然语言别名，例如 `LD normal`、`LD tile`
  - 时间范围，例如 `13-14 点`、`2026-04-15 13-14`
- 日志来源：
  - 消息文本中的 `http://` / `https://` 链接
  - 飞书消息附件里的 `file_xxx`
  - 本地路径 dry-run 调试
- 输出：
  - HTML 报告路径
  - JSON 报告路径
  - 飞书回复摘要

### 初版不支持

- 任意 shell 命令执行
- 自动 commit / push
- 删除文件
- 修改飞书权限
- 未放行群触发
- 没有日志输入时自动搜索客户文件
- 高风险写操作

## 三、架构拆分

```text
Feishu Bot Message
  -> EventConsumer
  -> MessageParser
  -> PolicyGuard
  -> TaskRouter
  -> LogDownloader
  -> SignalLifecycleHandler
  -> SignalChainRunner
  -> ReportPublisher
  -> LarkReplier
```

### 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| CLI | `lark_agent_bridge/cli.py` | 提供 `listen`、`handle-event`、`run-signal`、`check` 命令 |
| 配置 | `lark_agent_bridge/config.py` | 加载 TOML 配置，提供默认值 |
| 数据模型 | `lark_agent_bridge/models.py` | 定义事件、任务请求、下载资源、任务结果 |
| 消息解析 | `lark_agent_bridge/parser.py` | 从飞书消息内容中解析 signal、时间、URL、附件 key |
| 权限策略 | `lark_agent_bridge/policy.py` | 白名单群、私聊直通、命令前缀、风险等级 |
| 状态去重 | `lark_agent_bridge/state.py` | event_id 幂等和任务记录 |
| 飞书客户端 | `lark_agent_bridge/lark_client.py` | 封装 `lark-cli event/im` 命令 |
| 下载器 | `lark_agent_bridge/downloader.py` | 下载 HTTP 链接和飞书附件 |
| Runner | `lark_agent_bridge/runner.py` | 调用 guideengine skill 脚本 |
| 应用编排 | `lark_agent_bridge/app.py` | 串联解析、策略、下载、handler、回复 |
| 信号 handler | `lark_agent_bridge/handlers/signal_lifecycle.py` | 实现信号生命周期分析业务 |

## 四、详细任务拆分

### Task 1：补齐工程骨架和文档

目标：让工程具备清晰入口、配置样例和接手说明。

创建：

- `README.md`
- `config.example.toml`
- `lark_agent_bridge/cli.py`

验收：

- `python3 -m lark_agent_bridge --help` 能输出命令帮助。
- README 包含安装、配置、dry-run、真实运行、后台运行说明。

具体步骤：

1. 创建 `config.example.toml`，字段包括：
   - `dry_run`
   - `workspace_root`
   - `guideengine_repo`
   - `data_dir`
   - `allowed_chats`
   - `allowed_users`
   - `command_prefixes`
   - `signal_aliases`
   - `download.max_bytes`
   - `download.timeout_seconds`
   - `lark.reply_in_thread`
2. 创建 `README.md`，写清楚：
   - 项目目标
   - 当前只支持 `signal_lifecycle`
   - 启动监听命令
   - 本地样例事件命令
   - 安全边界
3. 创建 `cli.py`，先只实现空命令：
   - `check`
   - `handle-event`
   - `run-signal`
   - `listen`

### Task 2：实现模型和配置加载

目标：所有模块都用明确数据结构传递，不用到处传 dict。

创建：

- `lark_agent_bridge/models.py`
- `lark_agent_bridge/config.py`

模型建议：

- `BridgeConfig`
- `LarkEvent`
- `SignalRequest`
- `DownloadResource`
- `JobContext`
- `TaskResult`

验收：

- 能加载 `config.example.toml`。
- 缺省配置可在没有真实配置时进入 dry-run。
- `tests/test_config.py` 覆盖默认值和 TOML 覆盖。

### Task 3：实现消息解析

目标：从飞书事件内容中识别信号生命周期请求。

创建：

- `lark_agent_bridge/parser.py`
- `tests/test_parser.py`

解析规则：

- 数字信号：`\b\d{5,6}\b`
- 枚举信号：`SIGNAL_[A-Z0-9_]+`
- URL：`https?://\S+`
- 飞书文件 key：`file_[A-Za-z0-9_]+`
- 图片 key：`img_[A-Za-z0-9_]+`
- 时间范围：
  - `13-14 点` -> `13-14`
  - `2026-04-15 13-14` -> 原样传给 skill `--since`
- 业务触发词：
  - `/signal`
  - `信号生命周期`
  - `调查信号`
  - `有没有到 Unity`
  - `生命周期`

自然语言别名初版：

| 文案 | 解析为 |
|---|---|
| `LD normal` | `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA` |
| `normal over all` | `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA` |
| `LD tile` | `SIGNAL_X3D_LD_TILE_OVER_ALL_DATA` |
| `tile over all` | `SIGNAL_X3D_LD_TILE_OVER_ALL_DATA` |
| `SD over all` | `SIGNAL_X3D_SD_OVER_ALL_DATA` |

验收：

- 样例中文请求能解析到 signal。
- 含 URL 的请求能解析下载资源。
- 含 `file_xxx` 的请求能解析附件资源。
- 没有 signal 时返回明确错误，不调用 runner。

### Task 4：实现策略和状态去重

目标：避免 Bridge 被滥用或重复执行。

创建：

- `lark_agent_bridge/policy.py`
- `lark_agent_bridge/state.py`
- `tests/test_policy.py`

策略：

- 配置 `allowed_chats = []` 时表示不限制群，仅 dry-run 默认可用。
- 配置 `allowed_users = []` 时表示不限制用户；当前版本对允许群内成员默认放开，该字段保留为兼容配置。
- 只接受 group/p2p 消息，不接受未知 chat_type。
- 每个 `event_id` 只处理一次。

状态：

- 初版用 `data/state/seen_events.jsonl`。
- 写入字段：`event_id`、`message_id`、`sender_id`、`created_at`。

验收：

- 同一个 event_id 第二次处理被跳过。
- 非白名单群被拒绝，并返回明确提示。

### Task 5：封装飞书 CLI 客户端

目标：所有 `lark-cli` 调用集中封装，便于 dry-run 和测试替换。

创建：

- `lark_agent_bridge/lark_client.py`

封装命令：

- `consume_events()`
  - 执行：`lark-cli event consume im.message.receive_v1 --as bot`
  - 读取 stdout NDJSON
  - 等待 stderr ready marker
- `fetch_message(message_id)`
  - 执行：`lark-cli im +messages-mget --as bot --message-ids <id> --format json`
- `download_resource(message_id, file_key, type, output)`
  - 执行：`lark-cli im +messages-resources-download --as bot --message-id <id> --file-key <key> --type <type> --output <relative>`
- `reply(message_id, text, markdown=False)`
  - 执行：`lark-cli im +messages-reply --as bot --message-id <id> --text <text>`

注意：

- dry-run 时只打印要执行的命令，不真的调用飞书写接口。
- 真实回复默认用 `--reply-in-thread`，可以由配置关闭。
- `lark-cli` 可能需要 macOS Keychain 权限；正式运行应在非 sandbox 的用户 session 下。

验收：

- dry-run 模式不执行回复。
- `check` 命令能验证 `lark-cli --version` 和 `lark-cli auth status`。

### Task 6：实现下载器

目标：统一下载 HTTP 链接和飞书附件，保存到 job 目录。

创建：

- `lark_agent_bridge/downloader.py`
- `tests/test_downloader.py`

目录规范：

```text
tools/lark-agent-bridge/data/jobs/<job_id>/
  input/
  output/
  logs/
  job.json
```

HTTP 下载：

- 使用 Python 标准库 `urllib.request`
- 只允许 `http` / `https`
- 最大大小默认 5GB，可配置
- 超时默认 60 秒
- 文件名从 URL path 推断，不可信字符替换为 `_`

飞书附件下载：

- 调用 `LarkClient.download_resource`
- 输出到 `input/`

验收：

- dry-run 返回计划下载路径，不实际下载。
- URL 文件名清洗正确。
- 超出大小限制时报错。

### Task 7：实现 signal-chain runner

目标：调用 guideengine skill 脚本，并捕获 HTML/JSON 产物。

创建：

- `lark_agent_bridge/runner.py`
- `tests/test_runner.py`

命令：

```bash
python3 .github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py \
  --signal-code <signal> \
  --log-path <input_path> \
  --output <job_output/signal_chain.html> \
  --json-output <job_output/signal_chain.json>
```

如果有时间范围：

```bash
--since "<since>"
```

工作目录：

```text
/path/to/workspace/xp/guideengine/.worktrees/os6_xpdev
```

验收：

- dry-run 返回命令数组。
- 真实模式能生成明确输出路径。
- runner 超时后返回失败结果，不能卡死主循环。

### Task 8：实现 signal lifecycle handler

目标：串联解析结果、下载资源、调用 runner、生成回复。

创建：

- `lark_agent_bridge/handlers/signal_lifecycle.py`
- `tests/test_signal_handler.py`

流程：

1. 校验请求中有 signal。
2. 校验请求中有日志来源。
3. 创建 job 目录。
4. 下载日志到 `input/`。
5. 调 runner。
6. 生成摘要：
   - signal
   - 日志输入来源
   - HTML 报告路径
   - JSON 报告路径
   - 执行耗时
7. 返回 `TaskResult`。

失败回复：

- 缺 signal：提示支持 `132002` 或 `SIGNAL_...`
- 缺日志：提示可以发链接或附件
- 下载失败：提示下载错误
- runner 失败：提示 stderr 摘要和 job 路径

验收：

- dry-run 不下载、不跑 skill，只返回执行计划。
- 缺参数时返回清晰错误。

### Task 9：实现应用编排和 CLI

目标：完成端到端入口。

创建/修改：

- `lark_agent_bridge/app.py`
- `lark_agent_bridge/cli.py`

命令：

```bash
# 检查环境
python3 -m lark_agent_bridge check --config config.example.toml

# 处理一个样例事件
python3 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/signal_event_with_url.json \
  --dry-run

# 直接跑 signal handler，便于本地调试
python3 -m lark_agent_bridge run-signal \
  --config config.example.toml \
  --signal 132002 \
  --log-path /path/to/log \
  --dry-run

# 真实监听飞书消息
python3 -m lark_agent_bridge listen --config config.toml
```

验收：

- `handle-event` 能消费样例事件。
- `run-signal --dry-run` 输出 skill 命令。
- `listen` 能启动 `lark-cli event consume`，但本阶段可以不做真实长期运行验证。

### Task 10：样例事件和测试

目标：后续接手者不用真实飞书消息也能验证路由。

创建：

- `samples/signal_event_with_url.json`
- `samples/signal_event_with_file.json`
- `samples/signal_event_no_signal.json`
- `samples/signal_event_no_log.json`
- `tests/test_parser.py`
- `tests/test_policy.py`
- `tests/test_signal_handler.py`

样例事件字段采用 `im.message.receive_v1` schema：

```json
{
  "type": "im.message.receive_v1",
  "event_id": "evt_sample_001",
  "chat_id": "oc_sample",
  "chat_type": "group",
  "message_id": "om_sample",
  "sender_id": "ou_sample",
  "message_type": "text",
  "content": "@bot /signal 132002 日志 https://example.com/log.zip",
  "create_time": "1770000000000",
  "timestamp": "1770000000000"
}
```

验收命令：

```bash
cd /path/to/workspace/tools/lark-agent-bridge
python3 -m unittest discover -s tests -v
```

### Task 11：运行与后台化文档

目标：给后续部署留下明确路径。

创建：

- `docs/launchd.example.plist`
- `docs/operations.md`

内容：

- 前台运行命令
- macOS `launchctl` 后台运行模板
- 日志路径
- 停止服务
- 更新配置后重启
- 常见错误：
  - Keychain 访问失败
  - Bot 没在群里
  - 权限 scope 不足
  - 附件下载失败
  - skill 报告没有生成

## 五、验收标准

初版完成后必须满足：

- [x] `python3 -m unittest discover -s tests -v` 通过。
- [x] `python3 -m lark_agent_bridge check --config config.example.toml` 可运行。
- [x] `handle-event --dry-run` 对 URL 样例输出可执行计划。
- [x] `handle-event --dry-run` 对 file 样例输出飞书附件下载计划。
- [x] 缺 signal / 缺日志输入时给出清晰错误。
- [x] `run-signal --dry-run` 输出正确 `analyze_signal_chain.py` 命令。
- [x] README 能指导下一位接手者继续跑。

## 六、当前下一步

从这里继续时，建议先用真实 `config.toml` 配置白名单和 Feishu Bot 权限，再小范围试运行 `listen`。不要开放任意命令执行能力。
