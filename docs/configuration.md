# Configuration

`lark-agent-bridge` supports two configuration layers:

1. `config.toml`: local machine defaults
2. environment variables: sensitive or machine-specific overrides

The repository keeps `config.toml` as the only runtime TOML, and Git ignores it. `config/config.example.toml` is the committed desensitized template. Keep local credentials, user IDs, Bot IDs, internal URLs, and API keys out of committed files.

## Recommended setup

```bash
cp config/config.example.toml config.toml
./run.sh
./run.sh cc-switch-mimo-claude
```

Provider URL/model/protocol/agent/key-policy values live in `config/presets.toml`. Routing keyword tables live in `config/routing_terms.toml`. `run.sh <profile>` sets environment overrides for the current process while still loading `config.toml`.

## Python interpreter

`run.sh` picks the Python binary in this order:

1. `$LARK_AGENT_BRIDGE_PYTHON` — explicit override (e.g. a pyenv shim or corporate-managed interpreter).
2. `$PROJECT_DIR/.venv/bin/python` — the project virtualenv. Create once with `python3 -m venv .venv && .venv/bin/pip install -e .`.
3. `python3` from `$PATH` — last resort; only works when it already has the project's dependencies, including optional extras such as `pydantic-ai`.

Direct `python -m lark_agent_bridge ...` diagnostics should use the same interpreter `run.sh` would pick so optional capabilities (structured intent recognition, pydantic-ai runtime) do not silently degrade.

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
LARK_AGENT_BRIDGE_AI_ENABLED
LARK_AGENT_BRIDGE_AI_PRESET
LARK_AGENT_BRIDGE_AI_BASE_URL
LARK_AGENT_BRIDGE_AI_API_KEY
LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL
LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY
LARK_AGENT_BRIDGE_AGENT_PROVIDER
LARK_AGENT_BRIDGE_AGENT_COMMAND
```

Notes:

- `LARK_AGENT_BRIDGE_ALLOWED_CHATS` and `LARK_AGENT_BRIDGE_ALLOWED_USERS` are comma-separated lists
- `LARK_AGENT_BRIDGE_DRY_RUN` accepts `true/false`, `1/0`, `yes/no`, `on/off`
- environment variables win over `config.toml`
- `LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL` should point to the externally reachable report prefix, for example `https://bridge.example.com/reports`
- direct API profiles require `LARK_AGENT_BRIDGE_AI_API_KEY` or local `[ai_provider].api_key`; `run.sh` warns when both are empty

## Example shell setup

```bash
export LARK_AGENT_BRIDGE_ALLOWED_CHATS="oc_xxx,oc_yyy"
export LARK_AGENT_BRIDGE_BOT_NAME="My Feishu CLI Bot"
export LARK_AGENT_BRIDGE_OMLX_API_KEY="your-local-api-key"
export LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL="https://bridge.example.com/reports"
```

Clear inherited API variables before switching back to cc-switch or official CLI login:

```bash
unset LARK_AGENT_BRIDGE_AI_API_KEY LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY LARK_AGENT_BRIDGE_AI_BASE_URL LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL LARK_AGENT_BRIDGE_OMLX_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_OMLX_API_KEY
```

## Default agent selection

The bridge chooses the local Agent from the selected profile. `*-claude` maps to `claude`; `*-codex` maps to `codex`. For `codex-offi` and `claude-offi`, `run.sh` disables direct API and selects the matching CLI agent.

### Bug analysis default agent

Use `[bug_analysis]` only for execution settings. Leave `provider` and `command` empty unless you are intentionally overriding `run.sh`:

- bug final summary
- bug follow-up continuation
- bug reanalysis summary
- default provider fallback for `[intent_analysis]`

Example:

```toml
[bug_analysis]
enabled = true
provider = "codex"
command = "codex"
model = "gpt-5.4"
working_dir = "../.."
agent_summary_timeout_seconds = 300
resume_followup_sessions = false
auto_fallback_to_file_agent = false
```

Behavior:

- `provider = "codex"` means use Codex
- `provider = "claude"` means use Claude Code
- empty `provider` / `command` means derive from the selected preset protocol
- when `provider = "codex"`, the bridge passes `-m` using `model`; the default is `gpt-5.4`
- the bridge does not cross-fallback between Codex and Claude; keep `provider`, `command`, and the selected protocol route consistent
- `agent_summary_timeout_seconds` only limits the final Agent-written conclusion; if the Agent is silent longer than this window, the bridge falls back to the script summary and still returns the generated report
- `auto_fallback_to_file_agent = false` keeps direct API summary failures explicit instead of silently switching to Codex/Claude; set it to `true` only when you prefer automatic recovery over reproducible routing
- leave `default_prompt` empty unless you intentionally want a configured fallback; generic bug links should ask for a concrete analysis direction instead of silently defaulting to startup
- follow-ups and reanalysis create a fresh Agent session by default, while reusing the previous bug link, downloaded logs, extracted logs, report metadata, skill decision, and source-repo paths
- set `resume_followup_sessions = true` only if you explicitly want a follow-up to resume the old Agent session; reanalysis still uses a fresh Agent summary session so the new prompt and current skill/source evidence are not biased by stale private context
- each bug job writes `bug_agent_summary_prompt.md` and `bug_agent_summary_context.json` in the output directory so the exact Agent prompt and embedded local context can be audited
- multi-turn bug follow-ups now favor a stable `conversation_facts.json` snapshot over replaying the previous long-form summary; the snapshot stores stable facts, evidence entry points, and open questions, not the prior conclusion prose
- previous summary text is only reintroduced for explicit “compare/explain the old conclusion” follow-ups (for example `对比上一轮结论` or `你上次为什么判断...`); ordinary operational follow-ups such as “上次为什么没生成报告” do not pull the old summary back into the prompt

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
allow_subprocess_fallback = false
```

Behavior:

- if `provider` / `command` are empty here, intent routing reuses `[bug_analysis]`
- the bridge does not cross-fallback to the other intent-routing provider
- when `[ai_provider]` handles intent routing, subprocess fallback is disabled by default; set `allow_subprocess_fallback = true` only if you prefer slow legacy recovery over an explicit API failure
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
- local root copies of `config.*.toml`; committed config metadata/templates live under `config/`
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
- the same listener serves the local admin console at `/admin` (`/sessions` is an alias) and JSON APIs under `/api/sessions`, `/api/analysis-history`, `/api/cases`, `/api/skills`
- listener daemon health is exposed in `check` output and at `/api/daemon`
- agent timeline state for the console is stored under `data/state/agent_activity.json`

## Analysis history retention

Triggered log-analysis records are kept by default:

- `[job_retention].purge_all_on_listen_start = false`, so service restarts do not clear `data/jobs/*`
- periodic cleanup no longer removes historical job output, published reports, activity timelines, follow-up context, or case history
- cleanup still removes temporary Bug cache according to `bug_cache_max_age_hours`
- `/admin` exposes an analysis-history page backed by `/api/analysis-history`; deleting a record removes the linked activity session, case entry, job directory, published report, follow-up context, and report-version entry when present
- Bug/direct log analysis writes a focused `output/evidence_logs/manifest.json` bundle that preserves the selected navigation log (`log0`/`log1`/`log2`), its `logd`, and vehicle-related sibling logs when referenced by the investigation

## Knowledge QA

`[knowledge]` controls the personal knowledge QA and ADB simulation answers. On a fresh SQLite index, `/api/knowledge/sources` and `/api/knowledge/search` auto-sync configured sources instead of returning a misleading empty result. Verified and source-derived ADB simulation templates are matched before any model call; high-confidence Codex CLI source investigations can be recorded under `derived-adb-simulations` so later bot mentions and HTTP searches retrieve the verified template directly.

Knowledge auto-probe is intentionally narrow. `auto_probe_intent_terms` should contain generic action terms such as `模拟`、`adb`、`命令`、`广播`; do not put business aliases, signal names, codes, formats, values, or negative exclusions here. Those belong in the data-backed template store. If a broad operation question has no high-confidence template but the remaining core terms retrieve executable commands, the bridge can return those commands as low-confidence candidates and clearly ask the user to confirm the scenario. `[source_investigation]` is reserved for explicit requests such as `源码调查`、`查源码` or `基于源码`; ordinary fuzzy signal questions should use existing knowledge, derived templates, or no-hit guidance instead of automatically launching a model-backed source investigation.

`[source_investigation]` configures that fallback:

```toml
[source_investigation]
enabled = true
provider = "codex"
command = "codex"
model = "gpt-5.4"
fallback_model = "gpt-5.3-codex"
timeout_seconds = 120
max_evidence = 20
repo_roots = ["/path/to/guideengine"]
add_dirs = ["/path/to/Napa5"]
```

The runner invokes `codex exec --json --output-last-message -s read-only -m gpt-5.4`, sets `-C` to the primary repo root, and appends `--add-dir` for optional cross-repo lookups. The prompt tells Codex to use `rg` anchors first, read only key snippets, avoid whole-repo context dumps, and return a fixed JSON schema: `answer`, `canonical_key`, `confidence`, `commands`, `source_evidence`, `coverage_boundary`, `writeback_allowed`. Write-back is allowed only when confidence is high enough, a canonical key exists, and source evidence is present. This path is for short source investigations only; big logs, long reports, and bug-analysis summaries stay on the existing analysis runners.

`[codex_app_server]` enables an optional long-lived Codex runtime for file-agent style source analysis:

```toml
[codex_app_server]
enabled = false
command = "codex"
min_version = "0.125.0"
use_for_file_agent = false
use_for_bug_summary = false
fallback_to_exec = true
startup_timeout_seconds = 15
turn_timeout_seconds = 600
post_tool_quiet_timeout_seconds = 90
notification_poll_seconds = 0.25
max_event_audit = 200
sandbox_mode = "read-only"
disable_node_repl = true
disable_analytics = true
disable_memories = true
disable_apps_feature = true
disable_plugins_feature = true
disable_computer_use_feature = true
preserve_proxy_env = true
reasoning_effort = "medium"
use_minimal_home = true
```

This does **not** replace `codex exec`. When `enabled=true` and `use_for_file_agent=true`, the bridge tries `codex app-server` only for Codex-backed file-agent analysis, writes an event audit JSONL beside the analysis artifacts, and falls back to the existing `codex exec --json --output-last-message` path when the app-server run fails and `fallback_to_exec=true`.

The extra `disable_*`, `reasoning_effort`, and `use_minimal_home` knobs are there because this bridge uses app-server for bounded source-analysis turns, not for a full desktop-style Codex session. In practice:

- disabling `apps`, `plugins`, `computer_use`, `node_repl`, analytics, and memories reduces unrelated startup and network noise
- `preserve_proxy_env=true` re-injects the current shell's proxy variables into the app-server subprocess, even if the bridge's general `internal_network_env` policy would otherwise unset them; this matters on hosts where Codex backend access only works through a proxy
- `reasoning_effort="medium"` avoids carrying the user's global `xhigh` desktop default into these benchmark-sensitive file-agent runs
- `use_minimal_home=true` makes the bridge prepare a stripped-down `CODEX_HOME` under its own `data/` directory, copying only the auth/model cache files it needs, so app-server does not inherit the user's full desktop hooks/plugin state

Repeat source investigations now use a two-stage cache-friendly flow:

- the first non-local investigation of a question family does **not** write any reusable snapshot to disk
- the second real request of the same family can reuse an in-memory fact snapshot through the long-lived `KnowledgeService` source-investigation runner
- only after that repeated request succeeds does the bridge write a durable snapshot under `data/source_investigations/snapshots/`
- family matching prefers stable semantic identifiers from retrieved hits (`canonical_key`, `signal`, `code`) and only falls back to normalized question text when no better identifier exists
- local probe short-circuit answers do not participate in this snapshot flow because they never call the model/subprocess prompt path

## Event consumer health

`listen` runs `lark-cli event consume` as a managed subprocess. Configure it with `[event_consumer]`:

```toml
[event_consumer]
event_key = "im.message.receive_v1"
ready_timeout_seconds = 30
restart_on_failure = true
max_restarts = 0
restart_initial_delay_seconds = 1
restart_max_delay_seconds = 60
drop_stale_light_interactions = true
stale_light_interaction_grace_seconds = 120
```

Behavior:

- the bridge waits for the official stderr ready marker `[event] ready event_key=...` before treating the listener as healthy
- stdin is kept open with a Python pipe so `lark-cli event consume` does not exit immediately from stdin EOF under supervisors
- exit code `0` is treated as graceful completion and is not restarted
- non-zero startup/runtime failure is restarted when `restart_on_failure = true`
- `max_restarts = 0` means unlimited restarts with exponential backoff capped by `restart_max_delay_seconds`
- when lark-cli reconnects and replays old messages, `drop_stale_light_interactions = true` skips only stale help/identity/chat interactions created before listener readiness; Bug links, logs, files, signals, and follow-up analysis requests are not dropped by this guard
- the latest daemon status is stored in `data/state/agent_activity.json`
- card buttons that trigger bridge work require the `card.action.trigger` event. When the listener is configured for the default `im.message.receive_v1` message event, result cards hide Skill correction, reanalysis, continue-Agent, and feedback buttons so the UI does not expose inactive controls.

## Approval cards

`[approval]` controls the operation gate for high-impact paths:

```toml
[approval]
enabled = false
```

Keep approval disabled unless the deployment has a real card callback ingress wired into the bridge. The default `listen` path consumes `im.message.receive_v1`, so enabling approval without callback ingress will leave bug analysis, direct file analysis, and reanalysis waiting on `approval_pending` until the request expires. When enabled with callback ingress, the original event payload and route text are persisted in `data/state/approvals.json`; approve/reject card callbacks resume or cancel the pending operation.

## Workflow archive

`[workflow_archive]` is a best-effort Base/Doc/Drive sink for successful analysis results:

```toml
[workflow_archive]
enabled = false
base_token = ""
table_id = ""
drive_folder_token = ""
doc_parent_token = ""

[workflow_archive.base_field_map]
job_id = "任务ID"
mode = "分析类型"
status = "状态"
summary = "结论摘要"
report_url = "报告链接"
bug_url = "Bug链接"
provider = "Agent"
duration_seconds = "耗时秒"
chat_id = "群ID"
sender_id = "发起人"
doc_url = "Doc链接"
report_version = "报告版本"
```

Behavior:

- creates a Feishu Doc summary with `docs +create --api-version v2`
- uploads generated HTML/JSON reports to Drive with `drive +upload` when `drive_folder_token` is set
- writes a Base row with `base +record-upsert` when `base_token` and `table_id` are set
- archive failures are recorded under `result.details.workflow_archive`; they do not fail the completed analysis

## Notifications and dual-agent arbitration

Optional proactive push:

```toml
[notifications]
enabled = false
report_ready = true
```

When enabled, a de-duplicated "report ready" message is pushed after a report URL is published.

Optional dual-agent arbitration:

```toml
[dual_agent]
enabled = false
```

When enabled, if a runner returns `secondary_agent_summary` in `TaskResult.details`, the bridge compares the primary result with the secondary conclusion and writes the arbitration result back to `result.details.arbitration`.

## Intent routing

To let a local `codex` / `claude` decide whether a message is ordinary chat, a fresh analysis request, or a follow-up to an existing analysis session, enable `[intent_analysis]`:

```toml
[intent_analysis]
enabled = true
provider = "codex"
command = "codex"
model = "gpt-5.4"
working_dir = "../.."
timeout_seconds = 180
max_prompt_chars = 12000
```

Notes:

- if `provider` / `command` are empty, the bridge reuses `[bug_analysis]`
- when the selected provider is `codex`, the bridge passes `-m` using this block's `model`, or falls back to `[bug_analysis].model`
- the preferred provider is still selected from this block first; if it fails, the bridge tries the alternate provider automatically
- this agent only classifies intent; it does not replace the heavy local bug/log analyzers
- for bug follow-up messages, the classifier chooses whether the existing report context can answer or a reanalysis is needed; reanalysis reuses cached logs and source metadata but starts a fresh Agent summary session
- when `enabled = false`, the bridge falls back to the legacy deterministic routing rules

## Behavior permissions

Current behavior keeps group restrictions available, but does not restrict groups by default.

### Permission layers

1. `p2p`
   - private chats are allowed by default
2. `allowed_users`
   - super users; they bypass group allowlists
3. `allowed_chats = []`
   - default: all groups are fully authorized when the bot is explicitly mentioned
4. `allowed_chats = ["oc_xxx"]`
   - restricted mode: only listed groups are fully authorized
5. external groups in restricted mode
   - only limited log-analysis behavior is allowed for ordinary members

### What `allowed_chats` means

`allowed_chats = []` means no group restriction. In this default mode, any group can `@` the Bot for full use.

When `allowed_chats` is non-empty, it becomes a group allowlist. Only those groups are explicitly authorized for full use.

Inside unrestricted groups, or inside groups listed in `allowed_chats`, addressed messages can use:

- ordinary chat
- bug analysis
- direct file analysis
- signal analysis
- perception summary
- reply-based follow-up / reanalysis

### What `allowed_users` means

`allowed_users` means super-user bypass.

If `sender_id` is in `allowed_users`:

- the user can `@` the Bot in any group
- all abilities are available, not only log analysis
- if `[intent_analysis]` is enabled, those addressed messages still go through the local Agent route-classifier first

### External-group ordinary members in restricted mode

If `allowed_chats` is non-empty, a group is **not** in `allowed_chats`, and the sender is **not** in `allowed_users`, the bridge only allows log-analysis-oriented behavior:

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

### Local download-directory files

The bridge may resolve a bare filename from the configured local download directories only through the guarded `[local_resources]` path.

Default behavior:

- `enabled = true`
- `require_allowed_user = true`
- `allowed_dirs = ["~/Downloads"]`

All of the following must be true before a chat message can become a local file resource:

1. the sender is in `allowed_users`
2. the text explicitly mentions `下载目录`, `Downloads`, or `服务器下载目录`
3. the text contains a safe filename such as `Log.zip`
4. the file exists under one of `allowed_dirs`

Messages from other senders, or messages that contain only a filename without the download-directory authorization phrase, are not allowed to access local paths.

## Verification

Check the effective local configuration with:

```bash
.venv/bin/python -m lark_agent_bridge check --config config.toml
```
