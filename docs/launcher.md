# Multi-Config Launcher Guide

## 启动命令速查

```bash
./run.sh cc-switch      # cc-switch 代理（零配置）
./run.sh claude-oauth   # Claude 官方 OAuth（经 cc-switch）
./run.sh claude-key     # Anthropic API Key 直连
./run.sh openai-oauth   # OpenAI 官方 OAuth（经 cc-switch）
./run.sh openai-key     # OpenAI API Key 直连
./run.sh xiaomi-tp      # 小米 MiMo Token Plan（默认 config.toml）
./run.sh                # 默认 config.toml
```

其他启动脚本:
```bash
./run-openai.sh                    # OpenAI 兼容模式（支持 .env 加载 + --preset 覆盖）
./run-openai.sh --preset panda     # 覆盖 preset
scripts/run-listener.sh listen     # 手动运行（默认 config.toml）
scripts/run-listener.sh check      # 环境/配置诊断
scripts/run-listener.sh dry-run    # 验证不执行
LARK_AGENT_BRIDGE_CONFIG=config.cc-switch.toml scripts/run-listener.sh listen  # 指定配置
```

---

## 两种接入模式

### 模式 A: cc-switch 代理

```
bridge → Anthropic 格式 → cc-switch:15721 → [协议转换] → 后端供应商
```

- bridge 固定发 Anthropic 格式到 `127.0.0.1:15721`
- cc-switch 负责路由、协议转换、token 注入
- 在 cc-switch UI 里切换 provider，bridge 配置不用动
- `api_key = "PROXY_MANAGED"`（无需填真实 key）

### 模式 B: API Key 直连

```
bridge → [Anthropic 或 OpenAI 格式] → 供应商 API
```

- bridge 直接调用供应商 API
- 需要填 api_key（或环境变量 `LARK_AGENT_BRIDGE_AI_API_KEY`）
- 不依赖 cc-switch

---

## cc-switch 代理架构（重要）

### proxy 是 per app_type 排他的

cc-switch 的 `proxy_config` 表按 app_type 管理，**同一时刻只有一个 app_type 的 proxy 生效**：

```
app_type='claude'  listen_port=15721  enabled=1   ← 当前
app_type='codex'   listen_port=15721  enabled=0
app_type='gemini'  listen_port=15721  enabled=0
```

三个 app_type 共用同一个端口 `15721`，但同时只能开一个。

### 两层选择

在 cc-switch UI 里的操作分两层：

1. **选 app_type**：Claude / Codex / Gemini（决定 proxy 入口 + 接受的协议格式）
2. **选 provider**：该 app_type 下的某个供应商（NextApi / yybb / Codex OAuth 等）

### 协议约束

| proxy app_type | 接受的入口协议 | 能否转成 OpenAI 发给后端 |
|---|---|---|
| claude | Anthropic（`/v1/messages` + `x-api-key`） | 能，cc-switch 内部转 |
| codex | OpenAI（`/v1/chat/completions`） | — |
| gemini | Gemini 格式 | — |

**关键**：proxy 在 claude 时，bridge 只能发 Anthropic 格式。cc-switch 可以在后端转成 OpenAI，但入口协议锁死。如果必须用 OpenAI 协议入口，需要切 proxy 到 codex。

### Provider 挂载位置

Provider 可以挂在不同 app_type 下。例如你 cc-switch 里的数据：

```
"NextApi"        → app_type="claude",  Anthropic 格式, API Key 认证
"Codex"          → app_type="claude",  OpenAI Responses 格式, codex_oauth 认证
"codex-official" → app_type="codex",   OpenAI 格式, ChatGPT OAuth token
"YyBbCode"       → app_type="codex",   OpenAI 格式, API Key 认证
```

**"Codex" provider 虽然底层是 OpenAI 协议，但它挂在 claude app_type 下**，所以 proxy 在 claude 时也能用——cc-switch 会把入口的 Anthropic 请求转成 OpenAI Responses 给 chatgpt.com。

---

## Preset 详解

### 1. cc-switch

**Config**: `config.cc-switch.toml`
**协议**: Anthropic → cc-switch:15721

```bash
./run.sh cc-switch
```

- 零配置，无需 API key
- 在 cc-switch UI 切换 provider 即可改变后端
- 适用于开发/测试，灵活切换多个供应商

**要求**: cc-switch 运行中 (`lsof -i :15721`)

---

### 2. claude-oauth

**Config**: `config.claude-oauth.toml`
**协议**: Anthropic → cc-switch:15721 → Anthropic（或 cc-switch 转协议）

```bash
./run.sh claude-oauth
```

- 经 cc-switch 代理，cc-switch 管理 OAuth token 刷新和注入
- proxy 必须在 claude app_type
- 适用于使用 Anthropic 官方 OAuth 或挂在 claude app_type 下的其他 provider

**要求**: cc-switch 运行中 + proxy 在 claude + 已激活目标 provider

**注意**: 即使后端是 OpenAI 协议的 provider（如 "Codex"），只要它挂在 claude app_type 下就能用，cc-switch 会自动转协议。

---

### 3. claude-key

**Config**: `config.claude-key.toml`
**协议**: Anthropic → api.anthropic.com

```bash
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-ant-..."
./run.sh claude-key
```

- 直连 Anthropic API，无需 cc-switch
- 需要 Anthropic API Key: https://console.anthropic.com/settings/keys

---

### 4. openai-oauth

**Config**: `config.openai-oauth.toml`
**协议**: Anthropic → cc-switch:15721 → cc-switch 转 OpenAI → 后端

```bash
./run.sh openai-oauth
```

- 经 cc-switch 代理，bridge 发 Anthropic 格式，cc-switch 转协议给 OpenAI 后端
- proxy 必须在 claude app_type（bridge 入口是 Anthropic 格式）
- 适用于使用挂在 claude app_type 下的 OpenAI 协议 provider

**要求**: cc-switch 运行中 + proxy 在 claude + 已激活目标 provider

**与 claude-oauth 的区别**: 仅 `primary_model` 不同（`gpt-4.1` vs `claude-sonnet-4-6`），其他完全一样。都是发 Anthropic 格式到 cc-switch。

---

### 5. openai-key

**Config**: `config.openai-key.toml`
**协议**: OpenAI → api.openai.com

```bash
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-proj-..."
./run.sh openai-key
```

- 直连 OpenAI API，无需 cc-switch
- `api_format = "openai"`（走 `/v1/chat/completions`）
- 也支持 DeepSeek 等 OpenAI 兼容接口（改 preset）

---

### 6. xiaomi-tp（默认）

**Config**: `config.toml`
**协议**: Anthropic → token-plan-cn.xiaomimimo.com

```bash
export LARK_AGENT_BRIDGE_AI_API_KEY="tp-..."
./run.sh
```

- 直连小米 MiMo Token Plan
- `primary_model = "mimo-v2.5-pro"`
- 项目默认配置

---

## 配置文件总览

| 文件 | 接入方式 | 入口协议 | 需要 API Key | 需要 cc-switch | git |
|---|---|---|---|---|---|
| `config.cc-switch.toml` | cc-switch 代理 | Anthropic | 否 | 是 | ignored |
| `config.claude-oauth.toml` | cc-switch 代理 | Anthropic | 否 | 是 | tracked |
| `config.claude-key.toml` | Anthropic 直连 | Anthropic | 是 | 否 | ignored |
| `config.openai-oauth.toml` | cc-switch 代理 | Anthropic | 否 | 是 | tracked |
| `config.openai-key.toml` | OpenAI 直连 | OpenAI | 是 | 否 | ignored |
| `config.toml` | 小米 MiMo 直连 | Anthropic | 是 | 否 | ignored |
| `config.example.toml` | 参考模板 | — | — | — | tracked |

### claude-oauth vs openai-oauth

两者都是 cc-switch 代理模式，入口都是 Anthropic 格式到 `127.0.0.1:15721`。区别仅在于预设的 `primary_model`：

| | claude-oauth | openai-oauth |
|---|---|---|
| primary_model | `claude-sonnet-4-6` | `gpt-4.1` |
| fast_model | `claude-haiku-4-5-20251001` | `gpt-4.1-mini` |
| 适用场景 | Claude 系列模型 | GPT 系列模型 |

实际走哪个后端，完全取决于 cc-switch UI 里激活的是哪个 provider。

### cc-switch 代理 vs API Key 直连

| | cc-switch 代理 | API Key 直连 |
|---|---|---|
| 配置复杂度 | 需要 cc-switch 运行 | 只需 API Key |
| 切换供应商 | cc-switch UI 切换，bridge 不用动 | 改 TOML 配置或重启 |
| OAuth 支持 | cc-switch 管理 token 生命周期 | 不支持（需手动抓 token） |
| 协议转换 | cc-switch 自动转 | bridge 直接用目标协议 |
| 延迟 | 多一跳代理 | 直连 |
| 可用性 | 依赖 cc-switch 进程 | 依赖供应商 API |

---

## 所有配置共用的 section

除了 `[ai_provider]`，所有 preset TOML 共享相同的 section 结构：

- `[lark]` — 飞书 Bot 身份 (`bot_open_id`, `bot_name`)
- `[bug_analysis]` — Bug 分析引擎 (subprocess 阶段)
- `[intent_analysis]` — 意图分类
- `[claude_agent]` — Claude Code skill 集成
- `[source_investigation]` — 源码调查
- `[omlx_chat]` — 本地模型轻量聊天
- `[knowledge]` — 知识库 QA
- `[report_server]` — HTTP 报告服务
- `[download]` / `[job_retention]` / `[approval]` / `[notifications]`

---

## AI Provider Preset 列表

`[ai_provider].preset` 可选值（定义在 `config.py` 的 `_PROVIDER_PRESETS`）：

| Preset | Base URL | Primary Model | API Format | 需要 Key |
|---|---|---|---|---|
| `xiaomi-tp` | token-plan-cn.xiaomimimo.com | mimo-v2.5-pro | anthropic | 是 |
| `xiaomi-sk` | api.xiaomimimo.com | mimo-v2.5 | anthropic | 是 |
| `yybb` | yybb.codes | claude-sonnet-4-6 | anthropic | 是 |
| `yybb-codex` | hk.yybb.codes | gpt-5.5 | anthropic | 是 |
| `codex-official` | chatgpt.com/backend-api/codex | gpt-5.4 | anthropic | 是（需 cc-switch 注入） |
| `cc-switch` | 127.0.0.1:15721 | mimo-v2.5-pro | anthropic | 否 |
| `openai` | api.openai.com | gpt-4.1 | openai | 是 |
| `deepseek` | api.deepseek.com | deepseek-chat | openai | 是 |

使用方式：
```toml
[ai_provider]
enabled = true
preset = "yybb"
api_key = "sk-..."    # 或 env: LARK_AGENT_BRIDGE_AI_API_KEY
```

---

## 环境变量

| 变量 | 用途 |
|---|---|
| `LARK_AGENT_BRIDGE_AI_API_KEY` | AI 供应商 API Key |
| `LARK_AGENT_BRIDGE_AI_PRESET` | 覆盖 preset |
| `LARK_AGENT_BRIDGE_AI_BASE_URL` | 覆盖 base_url |
| `LARK_AGENT_BRIDGE_AI_PRIMARY_MODEL` | 覆盖 primary_model |
| `LARK_AGENT_BRIDGE_BOT_OPEN_ID` | 飞书 Bot Open ID |
| `LARK_AGENT_BRIDGE_BOT_NAME` | 飞书 Bot 名称 |
| `LARK_AGENT_BRIDGE_DRY_RUN` | 干跑模式 |
| `LARK_AGENT_BRIDGE_ADMIN_TOKEN` | 报告服务 Admin Token |
| `LARK_AGENT_BRIDGE_CONFIG` | run-listener.sh 配置文件路径 |

---

## 故障排查

### cc-switch 连接失败
```bash
lsof -i :15721                    # 检查 cc-switch 是否运行
```

### API Key 未设置
```bash
echo $LARK_AGENT_BRIDGE_AI_API_KEY
export LARK_AGENT_BRIDGE_AI_API_KEY="your-key"
```

### 配置文件不存在
```bash
ls config*.toml                   # 检查哪些配置文件存在
```

### OAuth token 过期
cc-switch 管理 OAuth token 生命周期。如果 token 过期：
1. 打开 cc-switch UI
2. 找到对应的 OAuth provider（如 "Codex"、"Claude Official"）
3. 重新登录刷新 token

### cc-switch proxy app_type 不匹配
如果报错或请求被拒，检查 cc-switch UI 里当前激活的 proxy app_type：
- `claude-oauth` / `openai-oauth` 都要求 proxy 在 **claude** app_type
- 如果 proxy 在 codex，需要先切换
