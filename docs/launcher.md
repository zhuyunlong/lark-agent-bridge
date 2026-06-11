# Launcher Guide

## 入口

仓库只保留一个运行入口和一个运行配置：

```bash
./run.sh
./run.sh cc-switch-mimo-claude
./run.sh yybb-codex
./run.sh claude-offi
```

`run.sh` 永远加载 `config.toml`。命令行参数只作为 profile/preset 覆盖。`config/config.example.toml` 是唯一提交到 Git 的脱敏模板；真实运行前复制成本地 `config.toml`。

## 统一规则

- 默认 profile 是 `deepseek-claude`。
- `*-claude` 代表 Anthropic 协议，本地 agent 固定为 `claude`。
- `*-codex` 代表 OpenAI 协议，本地 agent 固定为 `codex`。
- `cc-switch-*` 固定走 `http://127.0.0.1:15721`，key 由 cc-switch 管理。
- `codex-offi` / `claude-offi` 代表本机官方 CLI 网页登录凭证，关闭 direct API。
- 运行时不会在 `codex` 和 `claude` 之间跨 provider fallback。

## 支持的 Profile

| Profile | 接入方式 | 协议 | 本地 agent | 前提 |
|---|---|---|---|---|
| `cc-switch-deepseek-claude` | cc-switch -> DeepSeek | Anthropic | `claude` | cc-switch 激活 `claude` app_type + DeepSeek provider |
| `cc-switch-mimo-claude` | cc-switch -> MiMo | Anthropic | `claude` | cc-switch 激活 `claude` app_type + MiMo provider |
| `cc-switch-yybb-claude` | cc-switch -> yybb Claude | Anthropic | `claude` | cc-switch 激活 `claude` app_type + yybb Claude provider |
| `cc-switch-scihub-claude` | cc-switch -> SciHub Claude | Anthropic | `claude` | cc-switch 激活 `claude` app_type + SciHub provider |
| `cc-switch-yybb-codex` | cc-switch -> yybb Codex | OpenAI | `codex` | cc-switch 激活 `codex` app_type + yybb Codex provider |
| `deepseek-claude` | DeepSeek direct API | Anthropic | `claude` | `LARK_AGENT_BRIDGE_AI_API_KEY` |
| `mimo-claude` | MiMo direct API | Anthropic | `claude` | `LARK_AGENT_BRIDGE_AI_API_KEY` |
| `yybb-claude` | yybb Claude direct API | Anthropic | `claude` | `LARK_AGENT_BRIDGE_AI_API_KEY` |
| `yybb-codex` | yybb Codex direct API | OpenAI | `codex` | `LARK_AGENT_BRIDGE_AI_API_KEY` |
| `panda-codex` | TokenPanda direct API | OpenAI | `codex` | `LARK_AGENT_BRIDGE_AI_API_KEY` |
| `codex-offi` | Codex CLI login | CLI | `codex` | 本机 `codex` 已登录 |
| `claude-offi` | Claude CLI login | CLI | `claude` | 本机 `claude` 已登录 |

## 配置归属

- `config.toml`：唯一运行配置，必须脱敏，不提交 Git。
- `config/config.example.toml`：可提交的脱敏模板，不写真实 Bot ID、用户 ID、内部 URL 或 API key。
- `config/presets.toml`：集中管理 profile、provider base URL、模型名、协议格式、本地 agent、key 要求和 cc-switch `PROXY_MANAGED`。
- `config/routing_terms.toml`：集中管理路由关键词，不放在 Python 源码目录。
- `run.sh`：只负责选择 profile，并通过环境变量覆盖 `config.toml` 的 preset/agent。

## Direct API Key

`deepseek-claude`、`mimo-claude`、`yybb-claude`、`yybb-codex`、`panda-codex` 是 direct API profile，必须提供 `LARK_AGENT_BRIDGE_AI_API_KEY`，或者只在本机 ignored `config.toml` 写 `[ai_provider].api_key`。如果两者都没有，`run.sh` 会打印 warning，但仍继续启动，方便你马上看到当前缺失项。

清理当前 shell 和 launchd 中残留的 API 环境变量：

```bash
unset LARK_AGENT_BRIDGE_AI_API_KEY LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY LARK_AGENT_BRIDGE_AI_BASE_URL LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL LARK_AGENT_BRIDGE_OMLX_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_OMLX_API_KEY
```

## 检查

```bash
lsof -i :15721
.venv/bin/python -m lark_agent_bridge check --config config.toml
```

直接运行 `python -m lark_agent_bridge ...` 时优先使用项目虚拟环境。裸 `python3` 只有在装了与 `.venv` 相同依赖时才等价；否则可选能力（例如 `pydantic-ai` 的结构化意图识别）会自动降级。
