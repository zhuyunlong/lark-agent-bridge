# Lark Agent Bridge 当前进展

更新时间：2026-05-11

## 当前目标

实现一个本地飞书 Bot Bridge，当前支持两类消息：

- 信号生命周期调查：消息里给出 signal code、SignalCode 枚举名或自然语言描述，并提供日志下载链接或飞书附件。
- 基础聊天回复：例如 `你好`、`你是谁`、`帮助`。

## 已创建文件

```text
tools/lark-agent-bridge/
  docs/
    implementation-plan.md
    current-progress.md
    operations.md
    launchd.example.plist
  lark_agent_bridge/
    __init__.py
    __main__.py
    app.py
    cli.py
    config.py
    downloader.py
    lark_client.py
    models.py
    parser.py
    policy.py
    runner.py
    state.py
    handlers/
      __init__.py
      signal_lifecycle.py
  samples/
    signal_event_with_url.json
    signal_event_with_file.json
    signal_event_no_signal.json
    signal_event_no_log.json
  tests/
    test_*.py
  data/
  pyproject.toml
```

## 已写入内容

### `pyproject.toml`

已创建最小项目信息：

```toml
[project]
name = "lark-agent-bridge"
version = "0.1.0"
description = "Local Feishu Bot bridge for controlled AI agent tasks."
requires-python = ">=3.11"
```

当前测试使用标准库 `unittest`，没有引入第三方测试依赖。

### `lark_agent_bridge/__init__.py`

当前只有版本号：

```python
"""Local Feishu Bot bridge for controlled agent tasks."""

__version__ = "0.1.0"
```

### `lark_agent_bridge/__main__.py`

当前内容：

```python
from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

`cli.py` 已创建，`python3 -m lark_agent_bridge --help` 可输出命令帮助。

## 当前完成状态

```text
README.md
config.example.toml
lark_agent_bridge/cli.py
lark_agent_bridge/models.py
lark_agent_bridge/config.py
lark_agent_bridge/parser.py
lark_agent_bridge/policy.py
lark_agent_bridge/state.py
lark_agent_bridge/lark_client.py
lark_agent_bridge/downloader.py
lark_agent_bridge/runner.py
lark_agent_bridge/app.py
lark_agent_bridge/handlers/signal_lifecycle.py
samples/*.json
tests/*.py
docs/operations.md
docs/launchd.example.plist
```

这些文件已补齐，当前 dry-run 链路支持：

- `check`
- `handle-event`
- `run-signal`
- `listen` dry-run 提示和真实模式事件循环入口
- URL 日志计划下载
- 飞书 file/img 附件下载命令计划
- signal runner 命令计划
- 基础聊天回复路由
- 缺 signal / 缺日志的明确错误

## 关键外部依赖

### 飞书 CLI

当前机器已更新过：

```bash
lark-cli --version
# lark-cli version 1.0.27
```

事件监听命令：

```bash
lark-cli event consume im.message.receive_v1 --as bot
```

消息详情命令：

```bash
lark-cli im +messages-mget --as bot --message-ids om_xxx --format json
```

附件下载命令：

```bash
lark-cli im +messages-resources-download \
  --as bot \
  --message-id om_xxx \
  --file-key file_xxx \
  --type file \
  --output relative/path
```

消息回复命令：

```bash
lark-cli im +messages-reply \
  --as bot \
  --message-id om_xxx \
  --text "处理完成"
```

### guideengine signal-chain skill

仓库路径：

```text
/path/to/workspace/xp/guideengine/.worktrees/os6_xpdev
```

脚本：

```text
.github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py
```

调用形式：

```bash
python3 .github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py \
  --signal-code 132002 \
  --log-path /path/to/log/root \
  --output /path/to/job/output/signal_chain.html \
  --json-output /path/to/job/output/signal_chain.json
```

可选时间范围：

```bash
--since "2026-04-15 13-14"
```

## 设计决策

- 初版只做 `signal_lifecycle` 业务，不做通用远程 shell。
- 默认 dry-run，避免误发飞书消息或误下载大文件。
- 所有真实飞书操作集中在 `LarkClient`。
- 所有业务能力通过 `handlers/` 扩展。
- 下载文件只允许进入 `data/jobs/<job_id>/input/`。
- 输出报告进入 `data/jobs/<job_id>/output/`。
- 使用 `event_id` 去重，防止飞书事件重复投递。
- 真实运行前建议配置允许群列表；私聊默认可用，允许群内所有成员使用。

## 下一位接手者的建议起点

从 Task 1 继续：

```bash
cd /path/to/workspace/tools/lark-agent-bridge
```

优先创建：

```text
README.md
config.example.toml
lark_agent_bridge/cli.py
```

然后运行：

```bash
python3 -m lark_agent_bridge --help
```

预期：输出 CLI 帮助，而不是 `ModuleNotFoundError: No module named 'lark_agent_bridge.cli'`。

## 当前验证状态

已通过：

```bash
python3 -m unittest discover -s tests -v
python3 -m lark_agent_bridge check --config config.example.toml
python3 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_with_url.json --dry-run
python3 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_with_file.json --dry-run
python3 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_no_signal.json --dry-run
python3 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_no_log.json --dry-run
python3 -m lark_agent_bridge run-signal --config config.example.toml --signal 132002 --log-path /tmp/logs --dry-run
```

真实飞书监听尚未执行；上线前应使用 `config.toml` 配置白名单并确认 Bot 权限。

## 风险和注意事项

- `lark-cli` 在 sandbox 中可能遇到 `keychain Get failed: keychain not initialized`，真实运行要放在能访问 macOS Keychain 的用户会话里。
- `im.message.receive_v1` 事件是 bot 事件，目标群必须加 Bot，飞书开放平台也要开启对应事件。
- 真实附件下载需要 Bot 对消息和附件有权限。
- `signal-chain-analyzer` 会读取 guideengine 源码和日志，日志路径要可读，输出目录要可写。
- 初版不要打开任意命令执行能力。
