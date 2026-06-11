#!/bin/bash
# lark-agent-bridge single-config launcher
#
# Usage:
#   ./run.sh              # default profile from config/presets.toml
#   ./run.sh <profile>    # same config.toml, override profile

set -e

# Codux exposes the active AI CLI through DMUX wrappers in interactive shells.
# A long-running bridge listener must resolve its own agent binaries.
unset DMUX_ACTIVE_AI_RESOLVED_PATH DMUX_ACTIVE_AI_TOOL
unset DMUX_ACTIVE_AI_INVOCATION_ID DMUX_ACTIVE_AI_STARTED_AT

REQUESTED_PROFILE="${1:-default}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$PROJECT_DIR"

CONFIG="$PROJECT_DIR/config.toml"
PRESETS="$PROJECT_DIR/config/presets.toml"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ -n "${LARK_AGENT_BRIDGE_PYTHON:-}" ]; then
  PYTHON_BIN="$LARK_AGENT_BRIDGE_PYTHON"
elif [ -x "$VENV_PYTHON" ]; then
  PYTHON_BIN="$VENV_PYTHON"
else
  PYTHON_BIN="python3"
fi

if [ ! -f "$CONFIG" ]; then
  echo "Config not found: $CONFIG" >&2
  exit 1
fi

if [ ! -f "$PRESETS" ]; then
  echo "Profile registry not found: $PRESETS" >&2
  exit 1
fi

PROFILE_ENV="$("$PYTHON_BIN" -m lark_agent_bridge.profile_registry --path "$PRESETS" --shell "$REQUESTED_PROFILE")" || exit 1
eval "$PROFILE_ENV"

if [ "${PROFILE_REQUIRES_API_KEY:-false}" = "true" ] && [ -z "${LARK_AGENT_BRIDGE_AI_API_KEY:-}" ]; then
  CONFIG_AI_API_KEY="$("$PYTHON_BIN" -c 'import sys, tomllib; from pathlib import Path; config = Path(sys.argv[1]); profile = sys.argv[2]; data = tomllib.load(config.open("rb")); key = str(data.get("ai_provider", {}).get("api_key", "") or ""); secrets = config.parent / "config" / "secrets.toml";
if not key and secrets.exists():
    secret_data = tomllib.load(secrets.open("rb"))
    key = str((secret_data.get(profile) or {}).get("api_key", "") or "")
print(key)' "$CONFIG" "$PROFILE" 2>/dev/null || true)"
  if [ -z "$CONFIG_AI_API_KEY" ]; then
    echo "Warning: profile '$PROFILE' requires a direct API key." >&2
    echo "Set LARK_AGENT_BRIDGE_AI_API_KEY or local [ai_provider].api_key in config.toml." >&2
    if [ -n "${PROFILE_PRECONDITION:-}" ]; then
      echo "Profile precondition: $PROFILE_PRECONDITION" >&2
    fi
    echo "Clear shell API env vars: unset LARK_AGENT_BRIDGE_AI_API_KEY LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY LARK_AGENT_BRIDGE_AI_BASE_URL LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL LARK_AGENT_BRIDGE_OMLX_API_KEY" >&2
    echo "Clear launchd API env vars:" >&2
    echo "  launchctl unsetenv LARK_AGENT_BRIDGE_AI_API_KEY" >&2
    echo "  launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_API_KEY" >&2
    echo "  launchctl unsetenv LARK_AGENT_BRIDGE_AI_BASE_URL" >&2
    echo "  launchctl unsetenv LARK_AGENT_BRIDGE_AI_FALLBACK_BASE_URL" >&2
    echo "  launchctl unsetenv LARK_AGENT_BRIDGE_OMLX_API_KEY" >&2
  fi
fi

echo "Config: $CONFIG"
echo "Profiles: $PRESETS"
echo "Profile: $PROFILE"
echo "Agent: ${LARK_AGENT_BRIDGE_AGENT_PROVIDER:-auto}"
echo "Python: $PYTHON_BIN"
echo ""
echo "Press Ctrl+C to stop"
echo "---"

exec "$PYTHON_BIN" -m lark_agent_bridge listen --config "$CONFIG"
