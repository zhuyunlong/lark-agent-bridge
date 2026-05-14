# Configuration

`lark-agent-bridge` supports two configuration layers:

1. `config.toml`: local machine defaults
2. environment variables: sensitive or machine-specific overrides

The repository keeps only `config.example.toml`. Real `config.toml` is ignored by Git.

## Recommended setup

```bash
cp config.example.toml config.toml
```

Then choose one of these patterns:

- keep all values in `config.toml`
- keep non-sensitive defaults in `config.toml`, and inject sensitive values from environment variables

## Environment variables

Supported overrides:

```bash
LARK_AGENT_BRIDGE_DRY_RUN
LARK_AGENT_BRIDGE_WORKSPACE_ROOT
LARK_AGENT_BRIDGE_GUIDEENGINE_REPO
LARK_AGENT_BRIDGE_ALLOWED_CHATS
LARK_AGENT_BRIDGE_ALLOWED_USERS
LARK_AGENT_BRIDGE_BOT_OPEN_ID
LARK_AGENT_BRIDGE_BOT_NAME
LARK_AGENT_BRIDGE_OMLX_BASE_URL
LARK_AGENT_BRIDGE_OMLX_MODEL
LARK_AGENT_BRIDGE_OMLX_API_KEY
LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL
```

Notes:

- `LARK_AGENT_BRIDGE_ALLOWED_CHATS` and `LARK_AGENT_BRIDGE_ALLOWED_USERS` are comma-separated lists
- `LARK_AGENT_BRIDGE_DRY_RUN` accepts `true/false`, `1/0`, `yes/no`, `on/off`
- environment variables win over `config.toml`
- `LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL` should point to the externally reachable report prefix, for example `https://bridge.example.com/reports`

## Example shell setup

```bash
export LARK_AGENT_BRIDGE_ALLOWED_CHATS="oc_xxx,oc_yyy"
export LARK_AGENT_BRIDGE_BOT_NAME="My Feishu CLI Bot"
export LARK_AGENT_BRIDGE_OMLX_API_KEY="your-local-api-key"
export LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL="https://bridge.example.com/reports"
```

## Default agent selection

The bridge currently chooses the default local Agent at startup from `config.toml`.

### Bug analysis default agent

Use `[bug_analysis]` to define the preferred Agent for:

- bug final summary
- bug follow-up continuation
- bug reanalysis continuation
- default provider fallback for `[intent_analysis]`

Example:

```toml
[bug_analysis]
enabled = true
provider = "codex"
command = "codex"
working_dir = "../.."
```

or:

```toml
[bug_analysis]
enabled = true
provider = "claude"
command = "claude"
working_dir = "../.."
```

Behavior:

- `provider = "codex"` means prefer Codex first
- `provider = "claude"` means prefer Claude Code first
- if the preferred provider cannot start, returns non-zero, or cannot continue the summary session, the bridge automatically tries the other provider

### Intent routing default agent

Use `[intent_analysis]` only if you want a separate preferred Agent for route classification:

```toml
[intent_analysis]
enabled = true
provider = "claude"
command = "claude"
working_dir = "../.."
timeout_seconds = 180
max_prompt_chars = 12000
```

Behavior:

- if `provider` / `command` are empty here, intent routing reuses `[bug_analysis]`
- if the preferred intent-routing provider is unavailable, the bridge automatically tries the other provider
- this Agent only decides the route; heavy bug/log analysis still runs in local scripts

## Example launchd injection

If you run the bridge with `launchd`, prefer adding environment variables inside the plist instead of hardcoding them into tracked files.

Example:

```xml
<key>EnvironmentVariables</key>
<dict>
  <key>LARK_AGENT_BRIDGE_BOT_NAME</key>
  <string>My Feishu CLI Bot</string>
  <key>LARK_AGENT_BRIDGE_OMLX_API_KEY</key>
  <string>your-local-api-key</string>
</dict>
```

## Sensitive values

Keep these local:

- chat allowlists
- bot identifiers
- local API keys
- machine-specific absolute paths
- report server bind address / externally reachable report URL

Do not commit:

- `config.toml`
- `data/`
- generated reports, downloaded logs, or runtime state

## HTML report server

The bridge can publish generated HTML reports and reply with a link instead of uploading HTML/JSON files. Configure it with `[report_server]`:

```toml
[report_server]
enabled = true
bind_host = "0.0.0.0"
port = 8765
public_base_url = ""
```

Notes:

- `bind_host` / `port` control the local HTTP listener started by `listen`
- `public_base_url` is the URL written back into Feishu replies; if it is empty, or still uses `127.0.0.1` / `localhost`, the bridge rewrites it to the current LAN IP automatically
- `bind_host` uses `0.0.0.0` by default so peers inside the same LAN can open the generated report link
- published pages are stored under `data/published_reports/`
- reply-context state for follow-up questions is stored under `data/state/conversation_contexts.json`
- the same listener serves the local session console at `/sessions` and JSON APIs under `/api/sessions`
- agent timeline state for the console is stored under `data/state/agent_activity.json`

## Intent routing

To let a local `codex` / `claude` decide whether a message is ordinary chat, a fresh analysis request, or a follow-up to an existing analysis session, enable `[intent_analysis]`:

```toml
[intent_analysis]
enabled = true
provider = "codex"
command = "codex"
working_dir = "../.."
timeout_seconds = 180
max_prompt_chars = 12000
```

Notes:

- if `provider` / `command` are empty, the bridge reuses `[bug_analysis]`
- the preferred provider is still selected from this block first; if it fails, the bridge tries the alternate provider automatically
- this agent only classifies intent; it does not replace the heavy local bug/log analyzers
- for bug follow-up messages, the classifier chooses whether to continue the saved Bug agent session directly or trigger a reanalysis that still resumes the same provider session afterward
- when `enabled = false`, the bridge falls back to the legacy deterministic routing rules

## Behavior permissions

Current behavior is intentionally permissioned by chat type, group authorization, sender identity, and request class.

### Permission layers

1. `p2p`
   - private chats are allowed by default
2. `allowed_users`
   - super users; they bypass group allowlists
3. `allowed_chats`
   - fully authorized groups
4. external groups
   - only limited log-analysis behavior is allowed for ordinary members

### What `allowed_chats` means

`allowed_chats` means the Bot is explicitly authorized for full use in those groups.

Inside those groups, addressed messages can use:

- ordinary chat
- bug analysis
- direct file analysis
- signal analysis
- perception summary
- `/skill` read-only analysis
- reply-based follow-up / reanalysis

### What `allowed_users` means

`allowed_users` means super-user bypass.

If `sender_id` is in `allowed_users`:

- the user can `@` the Bot in any group
- all abilities are available, not only log analysis
- if `[intent_analysis]` is enabled, those addressed messages still go through the local Agent route-classifier first

### External-group ordinary members

If a group is **not** in `allowed_chats`, and the sender is **not** in `allowed_users`, the bridge only allows log-analysis-oriented behavior:

- bug link analysis
- reply-to-file direct analysis
- signal analysis
- perception summary
- explicit reply-based follow-up to an existing analysis

It does **not** allow ordinary free-form chat in those groups.

### Follow-up permissions

- group follow-up: must reply to the target message and mention the Bot
- p2p follow-up: must reply to the target message; `@` is not required
- the bridge no longer falls back to “the most recent analysis in the same chat”

### Reply-to-file direct analysis

When the current message itself does not contain `file_xxx`, the bridge can still resolve resources from the replied message:

1. fetch the replied message payload
2. extract `file_key` / `image_key`
3. download the resource using the source message ID

This is what enables:

```text
reply 某条文件消息
@bot 分析启动和卡顿
```

## Verification

Check the effective local configuration with:

```bash
python3.11 -m lark_agent_bridge check --config config.toml
```
