# Lark Agent Bridge

Local Feishu Bot bridge for controlled AI agent tasks. The current version supports these reply paths:

1. Signal lifecycle investigation: a Feishu message mentions the bot with a signal code/name/alias plus a log URL or attachment, then the bridge prepares the log input, calls the guideengine `signal-chain-analyzer`, publishes the generated HTML, and replies with a report link.
2. Bug investigation: a Feishu message mentions the bot and includes a `project.feishu.cn/.../buglo/detail/...` link plus a short description, then the bridge deterministically runs the local `feishu-bug-fetcher` pipeline and routes the Bug to one of these analysis types:
   - `unity-startup-lifecycle-check`: startup timing, first-frame, `UnityReady`, `displayChanged`, `startRender`
   - `3d-stuck-investigate`: 3D jank, freeze, black-screen, dropped-frame, ANR, render-not-refreshing
   - `3d-stuck-investigate` crash mode: crash, `tombstone`, `FATAL EXCEPTION`, native crash, app exit
   - `signal-chain-analyzer`: signal chain / data-not-reaching-Unity when the prompt or Bug text includes a concrete signal code or enum
   - `scene-signal-diagnosis`: scene type / gear / special-scene signal diagnosis
   - `perception-data-summary`: current perception data summary
   - `xtheme-analyzer`: XTheme / dawn / dusk / theme-switch diagnosis
   The bridge publishes a single HTML result page back to the chat instead of uploading local HTML/JSON artifacts.
3. Direct log analysis: a Feishu message mentions the bot with an attachment, Drive folder, image, URL, or authorized local download filename plus an analysis direction.
4. Perception summary: a Feishu message asks for current perception data summary with a log resource, then the bridge calls `perception-data-summary`.
5. Local omlx chat: short ordinary questions are sent to the local OpenAI-compatible omlx endpoint with model `gemma-4-26b-a4b-it-4bit` and a locally configured API key. This path has no local tool, file, shell, or Feishu permissions.
6. Personal knowledge QA: explicit `知识库`、`查知识` requests search the local SQLite knowledge index and reply with a card listing the answer and matched sources. `/kb` and `/qa` are kept as compatibility aliases. The first supported sources are local ADB JSON command files, Feishu Wiki docs, Feishu Base views, and guideengine signal-source snippets.
7. Basic bridge replies: identity and help prompts such as `你是谁`、`帮助`.

## Install

```bash
cd /path/to/workspace/tools/lark-agent-bridge

# One-time: create the project virtualenv and install the package + extras.
python3 -m venv .venv
.venv/bin/pip install -e .

# Verify the CLI loads from the venv.
.venv/bin/python -m lark_agent_bridge --help
```

Use the project virtualenv for runtime commands. `run.sh` resolves the Python interpreter in this order:

1. `$LARK_AGENT_BRIDGE_PYTHON` (explicit override, e.g. a pyenv/corporate interpreter)
2. `.venv/bin/python` (default project virtualenv)
3. `python3` from `$PATH` (last resort — only works when it already has the same dependencies, including optional extras such as `pydantic-ai`)

Direct `python -m lark_agent_bridge ...` invocations should use `.venv/bin/python` (or the same interpreter `run.sh` would pick) so optional extras stay available.

## Quick Start

Start the bot through the single launcher and single runtime config:

```bash
# Clean checkout setup: create the ignored local runtime file from the committed template.
cp config/config.example.toml config.toml

# Default: config.toml + cc-switch-deepseek-claude
./run.sh

# Override only the preset/profile. run.sh still loads config.toml.
./run.sh cc-switch-deepseek-claude
./run.sh cc-switch-mimo-claude
./run.sh cc-switch-yybb-claude
./run.sh cc-switch-yybb-codex
./run.sh cc-switch-scihub-claude
./run.sh deepseek-claude
./run.sh mimo-claude
./run.sh yybb-claude
./run.sh yybb-codex
./run.sh panda-codex
./run.sh codex-offi
./run.sh claude-offi
```

Stop the listener and the current `lark-cli` event bus:

```bash
./stop.sh
```

Or use the CLI directly with the same config and interpreter:

```bash
.venv/bin/python -m lark_agent_bridge listen --config config.toml
```

## Configure

`config.toml` is the only runtime TOML. It is ignored by Git and must stay desensitized: keep tokens, Bot IDs, user IDs, internal URLs, and API keys out of committed files. `config/config.example.toml` is the committed desensitized template.

Provider/profile details are centralized in `config/presets.toml`. Routing terms live in `config/routing_terms.toml`. Keep secrets in environment variables:

```bash
export LARK_AGENT_BRIDGE_BOT_NAME="My Feishu CLI Bot"
export LARK_AGENT_BRIDGE_OMLX_API_KEY="your-local-api-key"
export LARK_AGENT_BRIDGE_AI_API_KEY="your-provider-key"
```

Direct API profiles (`deepseek-claude`, `mimo-claude`, `yybb-claude`, `yybb-codex`, `panda-codex`) require `LARK_AGENT_BRIDGE_AI_API_KEY` or local `[ai_provider].api_key` in ignored `config.toml`. `run.sh` warns when neither is present.

Clear inherited API variables when switching back to cc-switch or official CLI login:

```bash
unset LARK_AGENT_BRIDGE_AI_API_KEY LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY LARK_AGENT_BRIDGE_AI_BASE_URL LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL LARK_AGENT_BRIDGE_OMLX_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY
launchctl unsetenv LARK_AGENT_BRIDGE_AI_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL
launchctl unsetenv LARK_AGENT_BRIDGE_OMLX_API_KEY
```

Detailed configuration guidance, environment variables, and `launchd` injection examples are in [docs/configuration.md](docs/configuration.md).

Reply behavior:

- `p2p` private chat: send a direct message back to the user
- `group` chat without a leading mention to this bot: silently skip, no reply
- `group` chat addressed to this bot: ordinary chat still sends a normal group message and `@` the sender
- HTML-producing analysis (signal, Bug 链接分析, 附件直传分析, 感知总结): reply to the triggering message with a dynamic progress card, update the same card with backend progress nodes, elapsed time, Agent token usage when available, and the final report link; in group chats also upload the HTML report; no JSON uploads
- when a user replies to the previous HTML result and `@` this bot, the bridge continues with the stored analysis context; simple follow-ups can answer from existing report metadata, while reanalysis reuses the previous Bug job, downloaded logs, extracted logs, skill decision, and source-repo paths. Agent summary runs start a fresh session by default; old Agent session resume is an explicit opt-in for rare diagnostic cases.
- rejected requests: still send back a clear error message
- addressed but unsupported requests: reply `not a handled request`
- job retention: triggered log-analysis jobs, published reports, activity timelines, follow-up context, and case history are preserved by default; cleanup only ages out temporary Bug cache

Common trigger examples are kept in the built-in `帮助` reply. Bug analysis routing is URL-based, and the short description controls the route:

```text
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D卡顿黑屏
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查闪退和tombstone
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析132002为什么没到Unity
@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 分析3D启动卡顿
@My Feishu CLI Bot 调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA 日志 file_xxx
@My Feishu CLI Bot 分析启动和卡顿 file_xxx 11:30
@My Feishu CLI Bot 总结当前感知数据 file_xxx
@My Feishu CLI Bot /chat 讲个笑话
@My Feishu CLI Bot 知识库 OTA信号如何模拟
@My Feishu CLI Bot 查知识 主题信号如何模拟
```

Signal alias / enum requests such as `SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA` trigger the `signal-chain-analyzer` skill.

Bug analysis uses `[bug_analysis]` for timeout and working directory, `[intent_analysis]` for optional agent-based message routing, and `[report_server]` for HTML publication. The current implementation does not depend on a local agent to complete the heavy bug-fetch/decode/report pipeline; it executes the local scripts directly for better stability, then hands the final conclusion to the configured Bug agent (`codex` / `claude`) when available. The final Agent summary is bounded by `agent_summary_timeout_seconds`; if that enhancement step is slow, the bridge falls back to the script summary and still returns the generated report. Each bug job writes `bug_agent_summary_prompt.md` and `bug_agent_summary_context.json` under its output directory so the exact Agent prompt and embedded context can be audited. When intent routing is enabled, a local agent can help classify ambiguous addressed messages; clear bug replies bypass that slow path, and reanalysis starts from cached logs plus current skill/source evidence rather than an old Agent session.

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
  - if none of the above match and the request has no concrete direction, the bridge asks the user to补充分析方向 instead of guessing a root cause
  - if the user provides concrete code/log scope, such as a class, function, package, PID, file path, or log keyword, the bridge may produce a bounded general triage report

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

Personal knowledge QA is backed by `[knowledge]`, `[source_investigation]`, and `data/knowledge/knowledge.sqlite`. Sync configured sources first:

```bash
.venv/bin/python -m lark_agent_bridge knowledge sync --config config.toml
.venv/bin/python -m lark_agent_bridge knowledge search --config config.toml "打开Debug面板"
.venv/bin/python -m lark_agent_bridge knowledge answer --config config.toml "OTA信号如何模拟"
.venv/bin/python -m lark_agent_bridge knowledge answer --config config.toml "主题信号如何模拟"
.venv/bin/python -m lark_agent_bridge knowledge answer --config config.toml "PB对象怎么ADB模拟"
```

The desensitized `config.toml` wires only the local guideengine signal-source snippets by default. Add private knowledge sources locally when needed; do not commit Feishu Wiki/Base URLs or local credential-bearing paths.

- guideengine signal-source snippets from `DataCenterBroadcastReceiver.java`、`SrOtaService.kt`、`signal.proto`

For fuzzy ADB signal simulation questions such as `OTA信号如何模拟`、`主题信号如何模拟` or `上下电如何模拟`, the bridge uses verified/source-derived template data first, then template alias/candidate matching, and only runs read-only Codex CLI source investigation when the user explicitly asks for source investigation. If a fuzzy topic maps to multiple candidate signals, the answer lists each candidate with a short use-case description and a follow-up phrase such as `知识库 模拟 SIGNAL_SR_XTHEME` for selecting one. Signal simulation is classified by source capability: primitive values can use the generic broadcast, verified `mockDataFactory` builders can return concrete templates, while ByteArray/PB/ProtoObject signals without a builder are reported as requiring custom factory code or record replay rather than a fake generic ADB command. When a broad operation question has no high-confidence template but the core terms retrieve executable commands, the bridge returns those commands as low-confidence candidates with an explicit confirmation boundary instead of falling back to unrelated high-noise entries. High-confidence Codex CLI findings with a canonical key and source evidence are recorded into `derived-adb-simulations`; low-confidence, timed-out, or invalid-schema runs only return the investigation boundary and are not written back. The same backend exposes `/api/knowledge/sources`, `/api/knowledge/search`, `/api/knowledge/sync`, and `/api/knowledge/items` for listing, searching, resyncing, and adding manual knowledge.

HTML links are served by the built-in report server. If `[report_server].public_base_url` is empty, or still points at loopback, the bridge will automatically switch to the current LAN IP. `listen` starts the local HTTP server in the background. The same server also exposes the local admin console at `/admin` and keeps `/sessions` as a compatibility alias. JSON APIs under `/api/sessions`, `/api/analysis-history`, `/api/cases`, `/api/skills`, `/api/knowledge`, `/api/daemon`, and `/api/health` let you inspect Bot conversations, previous investigations, latest report per Bug, skill definitions, knowledge entries, job IDs, report links, and agent progress stages from a browser.

`listen` also manages `lark-cli event consume` as a daemon-style subprocess: it waits for the official `[event] ready event_key=...` stderr marker, keeps stdin open so the consumer does not exit from EOF under supervisors, records the latest listener health in `data/state/agent_activity.json`, exposes it in `check` and `/api/daemon`, and restarts non-zero consumer failures with configurable backoff.

Interactive result cards are supported. Dynamic progress-card updates are best-effort and use `lark-cli api PATCH /open-apis/im/v1/messages/{message_id}`; if update fails, the bridge keeps the analysis running and falls back to the final result reply. Buttons that require callbacks, such as Skill correction, reanalysis, continuing an Agent, and useful/not-accurate feedback, are rendered only when the deployment is wired to consume `card.action.trigger`; the default `listen` path consumes `im.message.receive_v1`, so users should reply to the card with new requirements instead of relying on hidden callback buttons. Approval cards are available only when the deployment has a real card callback ingress wired into the bridge; keep `[approval].enabled = false` for the default `listen` path, otherwise medium/high-risk analysis requests will wait on `approval_pending` until the request expires. Successful reports can also be archived into Feishu Doc, Drive, and Base by enabling `[workflow_archive]`, and optional `[notifications]` / `[dual_agent]` switches add report-ready pushes and primary-vs-secondary conclusion arbitration.

To avoid cross-bot conflicts in busy groups, set `[lark].bot_name` or `[lark].bot_open_id` for this bot. Only that bot's leading mention should trigger group handling; messages that do not mention this bot are ignored.

Sensitive or machine-specific settings should stay out of Git:

- `config.toml` is ignored by default; committed config metadata/templates live under `config/`
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
  - `LARK_AGENT_BRIDGE_AI_API_KEY`
  - `LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY`
  - `LARK_AGENT_BRIDGE_AI_BASE_URL`
  - `LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL`
  - `LARK_AGENT_BRIDGE_REPORT_PUBLIC_BASE_URL`

Job retention is controlled by `[job_retention]`. The default local policy is:

- `purge_all_on_listen_start = false`: real `listen` restarts preserve prior `data/jobs/*`
- `max_age_hours = 6`: only temporary Bug cache uses age-based cleanup; historical job output and published reports are preserved until explicitly deleted
- `cleanup_interval_seconds = 60`: the running listener checks once per minute and cleans temporary cache
- `/admin` includes an analysis-history page backed by `/api/analysis-history`; deleting a record removes the activity session, linked case, job directory, published report, follow-up context, and report-version entry when present
- completed Bug/direct log analysis also writes `output/evidence_logs/manifest.json`, preserving the focused navigation log directory, its `logd`, and vehicle-related sibling logs when referenced by the investigation

## Dry-run examples

```bash
.venv/bin/python -m lark_agent_bridge check --config config.toml

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/signal_event_with_url.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/signal_event_with_file.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/basic_chat_who_are_you.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/omlx_chat_question.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/group_chat_command.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/group_unmentioned_url.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge handle-event \
  --config config.toml \
  --event samples/p2p_plain_chat.json \
  --dry-run

.venv/bin/python -m lark_agent_bridge run-signal \
  --config config.toml \
  --signal 132002 \
  --log-path /path/to/log \
  --dry-run
```

## Real run

Real mode requires a working `lark-cli` bot login and Feishu app scopes for message events, message reads, attachment downloads, and replies. `.venv/bin/python -m lark_agent_bridge check --config config.toml` will show the current `userOpenId` from `lark-cli auth status`.

```bash
./run.sh
```

For diagnostics without starting the listener:

```bash
.venv/bin/python -m lark_agent_bridge check --config config.toml
```

After `listen` starts, open `http://<bridge-lan-ip>:8765/admin` to view the local management console. It includes multi-session progress, historical cases with the latest report per Bug, listener health, and skill management for listing, creating, editing, deleting, and statically debugging local `.ai/skills` entries. `/sessions` still opens the same console for older bookmarks.

Do not expose this bridge as a generic shell executor. The supported behaviors are `signal_lifecycle`, Bug/direct log analysis, perception summary, local `omlx_chat`, follow-up/reanalysis, and a small set of bridge help replies.

## Background run

For a persistent macOS service, use a `launchd` plist that runs:

```bash
/absolute/path/to/lark-agent-bridge/.venv/bin/python -m lark_agent_bridge listen --config /absolute/path/to/config.toml
```

Run it from a normal user session so `lark-cli` can access macOS Keychain credentials.

## Tests

```bash
python3.11 -m unittest discover -s tests -v
```
