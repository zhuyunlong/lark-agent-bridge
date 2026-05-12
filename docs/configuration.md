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
```

Notes:

- `LARK_AGENT_BRIDGE_ALLOWED_CHATS` and `LARK_AGENT_BRIDGE_ALLOWED_USERS` are comma-separated lists
- `LARK_AGENT_BRIDGE_DRY_RUN` accepts `true/false`, `1/0`, `yes/no`, `on/off`
- environment variables win over `config.toml`

## Example shell setup

```bash
export LARK_AGENT_BRIDGE_ALLOWED_CHATS="oc_xxx,oc_yyy"
export LARK_AGENT_BRIDGE_BOT_NAME="My Feishu CLI Bot"
export LARK_AGENT_BRIDGE_OMLX_API_KEY="your-local-api-key"
```

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

Do not commit:

- `config.toml`
- `data/`
- generated reports, downloaded logs, or runtime state

## Verification

Check the effective local configuration with:

```bash
python3.11 -m lark_agent_bridge check --config config.toml
```
