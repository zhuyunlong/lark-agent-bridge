#!/bin/bash
# lark-agent-bridge multi-config launcher
# 用法:
#   ./run.sh cc-switch      # 使用 cc-switch preset (推荐给想要灵活切换 provider 的用户)
#   ./run.sh openai         # 使用 OpenAI direct API
#   ./run.sh               # 默认 cc-switch

set -e

PROFILE="${1:-cc-switch}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

case "$PROFILE" in
  cc-switch)
    CONFIG="$PROJECT_DIR/config.cc-switch.toml"
    echo "🔌 Starting lark-agent-bridge with cc-switch preset..."
    echo "   (cc-switch proxy at 127.0.0.1:15721 must be running)"
    echo "   Check: lsof -i :15721"
    ;;
  openai)
    CONFIG="$PROJECT_DIR/config.openai.toml"
    echo "🔑 Starting lark-agent-bridge with OpenAI direct API..."
    echo "   Requires: LARK_AGENT_BRIDGE_AI_API_KEY or config.openai.toml api_key"
    ;;
  *)
    echo "Usage: $0 {cc-switch|openai}" >&2
    echo "" >&2
    echo "Presets:" >&2
    echo "  cc-switch  - Use local cc-switch proxy (127.0.0.1:15721)" >&2
    echo "  openai     - Direct OpenAI API (requires API key)" >&2
    echo "" >&2
    echo "Other cc-switch presets available in code:" >&2
    echo "  xiaomi-tp, xiaomi-sk, yybb, yybb-codex, deepseek" >&2
    exit 1
    ;;
esac

if [ ! -f "$CONFIG" ]; then
  echo "❌ Config not found: $CONFIG" >&2
  exit 1
fi

echo "📝 Config: $CONFIG"
echo "📡 Reports: http://localhost:18888"
echo ""
echo "Press Ctrl+C to stop"
echo "---"

exec python3 -m lark_agent_bridge listen --config "$CONFIG"
