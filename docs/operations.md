# Lark Agent Bridge operations

## Foreground

```bash
cd /path/to/workspace/tools/lark-agent-bridge
./run.sh
```

Use only `config.toml` at runtime. Keep it desensitized and ignored by Git; `config/config.example.toml` is the committed desensitized template. By default `allowed_chats = []`, so group access is not restricted: any group can `@` the bot for bug analysis, direct analysis, signal analysis, perception summary, follow-up, and ordinary chat. Configure `allowed_chats` only when you want to turn on group access control.

Real deployments usually keep these values in local `config.toml` or environment variables:

- allowed groups: `allowed_chats = []` means no group restriction; `allowed_chats = ["oc_xxx", "oc_yyy"]` restricts full behavior to those groups
- super users: `allowed_users = ["ou_xxx"]`; these users bypass group allowlists and can use the Bot in any group
- `p2p` private chats: allowed for any user unless policy is tightened in code
- reply mode: private chat direct send; group messages only reply after a leading mention to this bot, then direct send with `@sender`
- group messages that do not mention this bot: silently skipped, no reply
- addressed but unsupported requests: reply `not a handled request`
- job retention: real `listen` startup preserves existing `data/jobs/*`; historical jobs, reports, activity timelines, follow-up context, and cases stay until explicitly deleted from the admin history page
- HTML analysis delivery: signal / bug / direct-analysis / perception flows reply to the triggering message with a dynamic progress card, update the same card with backend progress nodes, elapsed time, Agent token usage when available, and the final LAN-accessible HTML link; in group chats they also upload the HTML report file, but still do not upload JSON files
- dynamic progress cards are best-effort: updates use `lark-cli api PATCH /open-apis/im/v1/messages/{message_id}`; if the deployed bot cannot update card messages, the analysis still completes and the bridge falls back to the final result reply
- intent routing: when `[intent_analysis]` is enabled, a local `codex` / `claude` first classifies each addressed message as ordinary chat, a fresh analysis request, or a follow-up; for bug follow-up messages it also decides whether to continue the existing Bug agent session directly or trigger a reanalysis on the same saved job/log context
- bug follow-up: when the user replies to the previous result and `@`s the bot, the bridge continues with the saved analysis context and routes generic bug follow-up questions back into the configured Bug agent (`codex` / `claude`) instead of only doing context-chat replies; time-correction requests still reuse the previous prepared logs and job output instead of repeating fetch/download/decrypt steps, rerun only the affected analyzers, and then continue in the same agent session when the provider supports resume
- admin console: the report HTTP service serves `http://<bridge-lan-ip>:8765/admin` for multi-session progress, historical cases, skill management, report links, job IDs, and backend agent progress stages; `/sessions` remains an alias
- listener health: `listen` waits for the official `[event] ready event_key=...` marker, keeps the consumer stdin open to avoid EOF shutdown, records daemon health in `data/state/agent_activity.json`, and exposes it through `check` plus `http://<bridge-lan-ip>:8765/api/daemon`

## Default Agent

Profile selection is owned by `run.sh` and `config/presets.toml`:

- `./run.sh` defaults to `cc-switch-deepseek-claude`.
- `./run.sh <profile>` keeps `config.toml` and overrides only the profile.
- `*-claude` maps to `claude`; `*-codex` maps to `codex`.
- `codex-offi` / `claude-offi` use official CLI login and disable direct API.
- direct API profiles require `LARK_AGENT_BRIDGE_AI_API_KEY` or local `[ai_provider].api_key`; `run.sh` prints a warning if both are missing.

Leave provider and command empty in `config.toml` unless there is a deliberate local override:

```toml
[bug_analysis]
provider = ""
command = ""
agent_summary_timeout_seconds = 300

[intent_analysis]
provider = ""
command = ""
```

Behavior:

- the selected profile determines the only local Agent provider for that route
- if that provider cannot start or fails during the summary/continuation step, the bridge does not cross-fallback between Codex and Claude
- `agent_summary_timeout_seconds` bounds only the final Agent summary; when it expires, generated reports are still delivered using the script summary
- each bug job keeps `bug_agent_summary_prompt.md` and `bug_agent_summary_context.json` in `data/jobs/<job_id>/output/` for prompt/context audit

`[intent_analysis]` is optional. If enabled, it lets the selected local Agent classify each addressed message first.

## Behavior permissions

### Default group behavior

If `allowed_chats = []`, the Bot supports full behavior in any group where it is explicitly mentioned:

- ordinary chat
- bug analysis
- direct file analysis
- signal analysis
- perception summary
- reply follow-up / reanalysis

### Restricted full-permission groups

If `allowed_chats` is non-empty, only `chat_id` values in `allowed_chats` get full behavior:

- ordinary chat
- bug analysis
- direct file analysis
- signal analysis
- perception summary
- reply follow-up / reanalysis

### Super-user bypass

If `sender_id` is in `allowed_users`, that user has super permission:

- any group is allowed
- all abilities are allowed
- addressed messages still go through `[intent_analysis]` first when that classifier is enabled

### Non-allowlisted groups when restrictions are enabled

If `allowed_chats` is non-empty, and a group is not authorized and the sender is not a super user, the Bot still allows only log-analysis-oriented requests:

- bug links
- reply-to-file direct analysis
- signal analysis
- perception summary
- explicit reply follow-up to an existing analysis

Free-form ordinary chat is still blocked there.

## Dry-run checks

```bash
python3.11 -m lark_agent_bridge check --config config.toml
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/signal_event_with_url.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/signal_event_with_file.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/basic_chat_who_are_you.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/omlx_chat_question.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/group_chat_command.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/group_unmentioned_url.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.toml --event samples/p2p_plain_chat.json --dry-run
python3.11 -m lark_agent_bridge run-signal --config config.toml --signal 132002 --log-path /path/to/log --dry-run
```

Use Python 3.11+ for these commands. On this machine `/opt/homebrew/bin/python3.11` is available; the system `python3` may be older.

## Agent Routes

- Signal alias / enum requests such as `@My Feishu CLI Bot 调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA 日志 file_xxx` call the guideengine `signal-chain-analyzer`.
- Direct log-analysis requests such as `@My Feishu CLI Bot 分析启动和卡顿 file_xxx 11:30` route the uploaded file, image, folder, URL, or authorized local download file into the local analyzers.
- Perception requests such as `@My Feishu CLI Bot 总结当前感知数据 file_xxx` call `perception-data-summary`.
- A mentioned Feishu bug detail URL such as `@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序` calls the local bug-analysis pipeline. The bridge still runs the local bug-fetch/decode/analyzer scripts directly for the heavy work, but the final Bug conclusion is handed to the configured Bug agent (`codex` / `claude`), and both generic follow-ups and follow-up reanalysis try to stay in that same agent session.
- Private-chat ordinary questions such as `帮我解释一下什么是 token？`, or explicit mentioned group commands such as `@My Feishu CLI Bot /chat 讲个笑话` or `@My Feishu CLI Bot chat 讲个笑话`, call the local omlx OpenAI-compatible endpoint at `http://127.0.0.1:8000/v1`, model `gemma-4-26b-a4b-it-4bit`, and a locally configured API key. This route has no local tools or shell permissions.
- To avoid cross-bot conflicts, set `[lark].bot_name` or `[lark].bot_open_id` in `config.toml`; then only that bot's leading mention can trigger group handling.

## launchd background run

1. Copy `docs/launchd.example.plist` to `~/Library/LaunchAgents/com.local.lark-agent-bridge.plist`.
2. Replace the absolute paths if the workspace or Python path differs.
3. Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.local.lark-agent-bridge.plist
```

Stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.local.lark-agent-bridge.plist
```

Restart after config changes by unloading and loading the plist again.

## Logs and outputs

- Job input/output: `data/jobs/<job_id>/`
- Published report pages: `data/published_reports/<job_id>/`
- De-dup state: `data/state/seen_events.jsonl`
- Follow-up context state: `data/state/conversation_contexts.json`
- Session console state: `data/state/agent_activity.json`
- launchd stdout/stderr in the example plist: `data/logs/bridge.out.log` and `data/logs/bridge.err.log`
- Retention policy: `[job_retention] max_age_hours = 6`, `purge_all_on_listen_start = false`, `cleanup_interval_seconds = 60`; cleanup only ages out temporary Bug cache, not historical investigations
- Event consumer restart policy: `[event_consumer] restart_on_failure = true`, `max_restarts = 0`, `restart_initial_delay_seconds = 1`, `restart_max_delay_seconds = 60`

## Common errors

- `keychain Get failed` or auth errors: run from a normal macOS user session and refresh `lark-cli auth login`.
- Bot receives no messages: ensure the Bot is in the chat and the Feishu app has `im.message.receive_v1` enabled.
- Permission denied on message or attachment APIs: add the required IM scopes and re-authorize.
- Attachment download fails: verify the message ID, file key, Bot visibility, and resource type (`file` or `image`).
- Analyzer output missing: verify `guideengine_repo` points to the worktree containing `.github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py`.
- `tokenStatus` is `needs_refresh`: run `lark-cli auth login` before starting the real listener.
- Bug analysis fails to start: verify `config.toml [bug_analysis]`, local `claude` or `codex` availability, and Meegle auth state (`meegle auth status`).
- omlx chat returns unavailable: start omlx with `omlx serve --api-key <local-key>` or the equivalent `brew services` setup, and verify `curl http://127.0.0.1:8000/v1/models`.
- Listener starts and exits immediately: check `/api/daemon` or `data/state/agent_activity.json`; if stderr reports `reason: signal`, the parent likely closed stdin. The bridge now starts `lark-cli event consume` with stdin held open, so this usually means an external supervisor stopped the bridge itself.
