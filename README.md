# Lark Agent Bridge

Local Feishu Bot bridge for controlled AI agent tasks. The current version supports five kinds of replies:

1. Signal lifecycle investigation: a Feishu message mentions the bot with a signal code/name/alias plus a log URL or attachment, then the bridge prepares the log input, calls the guideengine `signal-chain-analyzer`, publishes the generated HTML, and replies with a report link.
2. Claude Code skill analysis: a Feishu message uses `skill` / `/skill` or `claude` / `/claude` as the first keyword, then the bridge calls the local `claude` CLI in a read-only analysis profile and uploads the Markdown result file back to the chat.
3. Bug investigation: a Feishu message mentions the bot and includes a `project.feishu.cn/.../buglo/detail/...` link plus a short description, then the bridge deterministically runs the local `feishu-bug-fetcher` pipeline and routes the Bug to one of these analysis types:
   - `unity-startup-lifecycle-check`: startup timing, first-frame, `UnityReady`, `displayChanged`, `startRender`
   - `3d-stuck-investigate`: 3D jank, freeze, black-screen, dropped-frame, ANR, render-not-refreshing
   - `3d-stuck-investigate` crash mode: crash, `tombstone`, `FATAL EXCEPTION`, native crash, app exit
   - `signal-chain-analyzer`: signal chain / data-not-reaching-Unity when the prompt or Bug text includes a concrete signal code or enum
   The bridge publishes a single HTML result page back to the chat instead of uploading local HTML/JSON artifacts.
4. Local omlx chat: short ordinary questions are sent to the local OpenAI-compatible omlx endpoint with model `gemma-4-26b-a4b-it-4bit` and a locally configured API key. This path has no local tool, file, shell, or Feishu permissions.
5. Basic bridge replies: identity and help prompts such as `你是谁`、`帮助`.

## Install

```bash
cd /path/to/workspace/tools/lark-agent-bridge
python3.11 -m lark_agent_bridge --help
```

The project intentionally uses Python standard library modules only and requires Python 3.11+.

## Configure

Copy the example and edit allowlists before real use:

```bash
cp config.example.toml config.toml
```

`dry_run = true` is the safe default. Group access is controlled by `allowed_chats`; users inside an allowed group are all supported. `p2p` private chats are also supported directly.

Recommended local setup:

```bash
cp config.example.toml config.toml
```

Then keep sensitive values in either `config.toml` or environment variables:

```bash
export LARK_AGENT_BRIDGE_ALLOWED_CHATS="oc_xxx,oc_yyy"
export LARK_AGENT_BRIDGE_BOT_NAME="My Feishu CLI Bot"
export LARK_AGENT_BRIDGE_OMLX_API_KEY="your-local-api-key"
```

Detailed configuration guidance, environment variables, and `launchd` injection examples are in [docs/configuration.md](docs/configuration.md).

Reply behavior:

- `p2p` private chat: send a direct message back to the user
- `group` chat without a leading mention to this bot: silently skip, no reply
- `group` chat addressed to this bot: ordinary chat still sends a normal group message and `@` the sender
- Claude Code skill analysis: send a text excerpt first, then upload `claude_skill_result.md` as a file
- HTML-producing analysis (`/signal`, Bug 链接分析, 附件直传分析, 感知总结): reply to the triggering message with `@` the sender, publish one LAN-accessible HTML link, and in group chats also upload the HTML report; no JSON uploads
- when a user replies to the previous HTML result and `@` this bot, the bridge continues the conversation with the stored analysis context; if the user corrects the problem time and asks to re-analyse, the bridge reuses the previous Bug job and prepared log input instead of fetching/downloading/decrypting again, refreshes only the affected reports, and then lets the configured Bug agent (`codex` / `claude`) continue the same summary session when the provider supports resume
- rejected requests: still send back a clear error message
- addressed but unsupported requests: reply `not a handled request`
- job retention: `listen` startup purges old `data/jobs/*`; while the service keeps running, a background cleanup loop removes job directories older than 6 hours by default

Claude Code routing is deliberately prefix-based by default:

```text
/skill 分析 tools/lark-agent-bridge 当前支持哪些安全边界
skill 分析 tools/lark-agent-bridge 当前支持哪些安全边界
/claude 帮我 review 这个目录的实现思路
claude 帮我 review 这个目录的实现思路
```

Bug analysis routing is URL-based, and the short description controls the route:

```text
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D卡顿黑屏
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查闪退和tombstone
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析132002为什么没到Unity
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析3D启动卡顿
```

The default Claude Code tool allowlist is read-only: `Read`, `Grep`, `Glob`, `LS`. It does not grant edit or shell execution permissions unless you change `config.toml`.

Bug analysis uses `[bug_analysis]` for timeout and working directory, `[intent_analysis]` for optional agent-based message routing, and `[report_server]` for HTML publication. The current implementation does not depend on a local agent to complete the heavy bug-fetch/decode/report pipeline; it executes the local scripts directly for better stability, then hands the final conclusion to the configured Bug agent (`codex` / `claude`) when available. When intent routing is enabled, a local agent first decides whether an addressed message is ordinary chat, a fresh analysis request, or a follow-up that should continue the same saved Bug agent session.

Current Bug routing rules:

- startup route:
  - trigger words such as `启动` `时序` `首帧` `UnityReady` `displayChanged` `startRender`
  - output files: `bug_3d_startup_report.html` and `.json`
- stuck route:
  - trigger words such as `卡顿` `卡住` `卡死` `掉帧` `黑屏` `ANR` `不刷新`
  - output files: `bug_3d_stuck_report.html` and `.json`
- startup + stuck combined route:
  - when the prompt matches both startup and stuck, the bridge still runs both underlying analyses
  - publishes one merged report page backed by `bug_startup_stuck_report.html`
  - the per-skill reports remain in the job directory and published bundle for drill-down
- crash route:
  - trigger words such as `闪退` `crash` `tombstone` `FATAL EXCEPTION` `异常退出` `SIGSEGV`
  - current implementation reuses `3d-stuck-investigate` as the execution engine and exports `bug_crash_report.html` and `.json`
- signal route:
  - trigger words such as `信号` `数据链` `链路` `没到Unity` plus a concrete signal code like `132002` or enum like `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA`
  - output files: `bug_signal_chain_report.html` and `.json`
- fallback:
  - if none of the above match, the bridge falls back to startup analysis

Ordinary short questions that do not look like signal/log/code-analysis tasks go to omlx in private chat. In group chats, mention this bot first and then use `chat` or `/chat` as the first keyword:

```text
私聊: 帮我解释一下什么是 token？
群聊: @My Feishu CLI Bot /chat 讲个笑话
群聊: @My Feishu CLI Bot chat 讲个笑话
```

The omlx path is only an HTTP chat-completions call:

```toml
[omlx_chat]
base_url = "http://127.0.0.1:8000/v1"
model = "gemma-4-26b-a4b-it-4bit"
api_key = ""
```

HTML links are served by the built-in report server. If `[report_server].public_base_url` is empty, or still points at loopback, the bridge will automatically switch to the current LAN IP. `listen` starts the local HTTP server in the background. The same server also exposes the local session console at `/sessions`, with JSON APIs under `/api/sessions`, so you can inspect Bot conversations, job IDs, report links, and agent progress stages from a browser.

To avoid cross-bot conflicts in busy groups, set `[lark].bot_name` or `[lark].bot_open_id` for this bot. Only that bot's leading mention should trigger group handling; messages that do not mention this bot are ignored.

Sensitive or machine-specific settings should stay out of Git:

- `config.toml` is ignored by default
- `data/` runtime outputs are ignored by default
- supported environment variables:
  - `LARK_AGENT_BRIDGE_WORKSPACE_ROOT`
  - `LARK_AGENT_BRIDGE_GUIDEENGINE_REPO`
  - `LARK_AGENT_BRIDGE_ALLOWED_CHATS`
  - `LARK_AGENT_BRIDGE_ALLOWED_USERS`
  - `LARK_AGENT_BRIDGE_BOT_OPEN_ID`
  - `LARK_AGENT_BRIDGE_BOT_NAME`
  - `LARK_AGENT_BRIDGE_OMLX_BASE_URL`
  - `LARK_AGENT_BRIDGE_OMLX_MODEL`
  - `LARK_AGENT_BRIDGE_OMLX_API_KEY`
  - `LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL`

Job retention is controlled by `[job_retention]`. The default local policy is:

- `purge_all_on_listen_start = true`: every real `listen` start clears prior `data/jobs/*`
- `max_age_hours = 6`: no job output is meant to be kept beyond 6 hours
- `cleanup_interval_seconds = 60`: the running listener checks once per minute and removes expired job directories

## Dry-run examples

```bash
python3.11 -m lark_agent_bridge check --config config.example.toml

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/signal_event_with_url.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/signal_event_with_file.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/basic_chat_who_are_you.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/claude_skill_request.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/omlx_chat_question.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/group_chat_command.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/group_unmentioned_url.json \
  --dry-run

python3.11 -m lark_agent_bridge handle-event \
  --config config.example.toml \
  --event samples/p2p_plain_chat.json \
  --dry-run

python3.11 -m lark_agent_bridge run-signal \
  --config config.example.toml \
  --signal 132002 \
  --log-path /path/to/log \
  --dry-run
```

## Real run

Real mode requires a working `lark-cli` bot login and Feishu app scopes for message events, message reads, attachment downloads, and replies. `python3.11 -m lark_agent_bridge check --config config.toml` will show the current `userOpenId` from `lark-cli auth status`.

```bash
python3.11 -m lark_agent_bridge listen --config config.toml
```

After `listen` starts, open `http://<bridge-lan-ip>:8765/sessions` to view the local conversation and agent-progress console. The page polls the local API and uses the same retention policy as jobs/reports.

Do not expose this bridge as a generic shell executor. The supported behaviors are `signal_lifecycle`, read-only `claude_skill`, local `omlx_chat`, and a small set of bridge help replies.

## Background run

For a persistent macOS service, use a `launchd` plist that runs:

```bash
python3.11 -m lark_agent_bridge listen --config /absolute/path/to/config.toml
```

Run it from a normal user session so `lark-cli` can access macOS Keychain credentials.

## Tests

```bash
python3.11 -m unittest discover -s tests -v
```
