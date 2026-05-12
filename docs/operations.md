# Lark Agent Bridge operations

## Foreground

```bash
cd /path/to/workspace/tools/lark-agent-bridge
python3.11 -m lark_agent_bridge listen --config config.toml
```

Use `config.example.toml` only for dry-run checks. For real Feishu traffic, copy it to `config.toml`, keep `dry_run = false`, and configure `allowed_chats` for **group** access control.

Real deployments usually keep these values in local `config.toml` or environment variables:

- allowed groups: `allowed_chats = ["oc_xxx", "oc_yyy"]`
- allowed group users: all members in the allowed groups, unless `allowed_users` is further restricted
- `p2p` private chats: allowed for any user unless policy is tightened in code
- reply mode: private chat direct send; group messages only reply after a leading mention to this bot, then direct send with `@sender`
- group messages that do not mention this bot: silently skipped, no reply
- addressed but unsupported requests: reply `not a handled request`
- job retention: real `listen` startup clears existing `data/jobs/*`; while running, expired job directories are removed automatically
- bug analysis: a group-mentioned `project.feishu.cn/.../buglo/detail/...` link can trigger local bug fetching, log decode, startup analysis, and file upload

## Dry-run checks

```bash
python3.11 -m lark_agent_bridge check --config config.example.toml
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_with_url.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/signal_event_with_file.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/basic_chat_who_are_you.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/claude_skill_request.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/omlx_chat_question.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/group_chat_command.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/group_unmentioned_url.json --dry-run
python3.11 -m lark_agent_bridge handle-event --config config.example.toml --event samples/p2p_plain_chat.json --dry-run
python3.11 -m lark_agent_bridge run-signal --config config.example.toml --signal 132002 --log-path /path/to/log --dry-run
```

Use Python 3.11+ for these commands. On this machine `/opt/homebrew/bin/python3.11` is available; the system `python3` may be older.

## Agent Routes

- `/signal ...` keeps using the guideengine `signal-chain-analyzer`.
- `skill ...` / `/skill ...` or `claude ...` / `/claude ...` calls local Claude Code for read-only skill analysis. The keyword must be the first token. The default tool allowlist is `Read`, `Grep`, `Glob`, `LS`; result Markdown is sent back as a file when not in dry-run mode.
- A mentioned Feishu bug detail URL such as `@My Feishu CLI Bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序` calls the local bug-analysis pipeline. The current implementation directly runs the local bug-fetch/decode/startup-analysis scripts and uploads `bug_metadata.md` plus the HTML report.
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
- De-dup state: `data/state/seen_events.jsonl`
- launchd stdout/stderr in the example plist: `data/logs/bridge.out.log` and `data/logs/bridge.err.log`
- Retention policy: `[job_retention] max_age_hours = 6`, `purge_all_on_listen_start = true`, `cleanup_interval_seconds = 60`

## Common errors

- `keychain Get failed` or auth errors: run from a normal macOS user session and refresh `lark-cli auth login`.
- Bot receives no messages: ensure the Bot is in the chat and the Feishu app has `im.message.receive_v1` enabled.
- Permission denied on message or attachment APIs: add the required IM scopes and re-authorize.
- Attachment download fails: verify the message ID, file key, Bot visibility, and resource type (`file` or `image`).
- Analyzer output missing: verify `guideengine_repo` points to the worktree containing `.github/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py`.
- `tokenStatus` is `needs_refresh`: run `lark-cli auth login` before starting the real listener.
- Claude Code analysis fails to start: run `claude --help` in the same user session and verify `config.toml [claude_agent].command`.
- Bug analysis fails to start: verify `config.toml [bug_analysis]`, local `claude` or `codex` availability, and Meegle auth state (`meegle auth status`).
- omlx chat returns unavailable: start omlx with `omlx serve --api-key <local-key>` or the equivalent `brew services` setup, and verify `curl http://127.0.0.1:8000/v1/models`.
