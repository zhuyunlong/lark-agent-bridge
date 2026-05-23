#!/bin/bash
# lark-agent-bridge multi-config launcher
# 用法:
#   ./run.sh cc-switch      # cc-switch 代理（零配置）
#   ./run.sh claude-oauth   # Claude 官方 OAuth（经 cc-switch）
#   ./run.sh claude-key     # Anthropic API Key 直连
#   ./run.sh openai-oauth   # OpenAI 官方 OAuth（经 cc-switch）
#   ./run.sh openai-key     # OpenAI API Key 直连
#   ./run.sh xiaomi-tp      # 小米 MiMo Token Plan（默认）
#   ./run.sh                # 默认 config.toml

set -e

PROFILE="${1:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

case "$PROFILE" in
  cc-switch)
    CONFIG="$PROJECT_DIR/config.cc-switch.toml"
    echo "🔌 Starting with cc-switch proxy..."
    echo "   cc-switch must be running at 127.0.0.1:15721"
    ;;
  claude-oauth)
    CONFIG="$PROJECT_DIR/config.claude-oauth.toml"
    echo "🔑 Starting with Claude Official OAuth (via cc-switch)..."
    echo "   cc-switch must be running with 'Claude Official' provider active"
    ;;
  claude-key)
    CONFIG="$PROJECT_DIR/config.claude-key.toml"
    echo "🔑 Starting with Anthropic API Key..."
    echo "   Requires: LARK_AGENT_BRIDGE_AI_API_KEY or config.claude-key.toml api_key"
    ;;
  openai-oauth)
    CONFIG="$PROJECT_DIR/config.openai-oauth.toml"
    echo "🔑 Starting with OpenAI Official OAuth (via cc-switch)..."
    echo "   cc-switch must be running with 'OpenAI Official' provider active"
    ;;
  openai-key)
    CONFIG="$PROJECT_DIR/config.openai-key.toml"
    echo "🔑 Starting with OpenAI API Key..."
    echo "   Requires: LARK_AGENT_BRIDGE_AI_API_KEY or config.openai-key.toml api_key"
    ;;
  xiaomi-tp|default)
    CONFIG="$PROJECT_DIR/config.toml"
    echo "🔌 Starting with Xiaomi MiMo Token Plan (default config.toml)..."
    echo "   Requires: LARK_AGENT_BRIDGE_AI_API_KEY or config.toml api_key"
    ;;
  "")
    CONFIG="$PROJECT_DIR/config.toml"
    echo "🔌 Starting with default config.toml..."
    ;;
  *)
    echo "Usage: $0 {cc-switch|claude-oauth|claude-key|openai-oauth|openai-key|xiaomi-tp}" >&2
    echo "" >&2
    echo "Presets:" >&2
    echo "  cc-switch     - cc-switch 代理，零配置" >&2
    echo "  claude-oauth  - Claude 官方 OAuth（经 cc-switch）" >&2
    echo "  claude-key    - Anthropic API Key 直连" >&2
    echo "  openai-oauth  - OpenAI 官方 OAuth（经 cc-switch）" >&2
    echo "  openai-key    - OpenAI API Key 直连" >&2
    echo "  xiaomi-tp     - 小米 MiMo Token Plan（默认）" >&2
    exit 1
    ;;
esac

if [ ! -f "$CONFIG" ]; then
  echo "❌ Config not found: $CONFIG" >&2
  exit 1
fi

echo "📝 Config: $CONFIG"
echo ""
echo "Press Ctrl+C to stop"
echo "---"

exec python3 -m lark_agent_bridge listen --config "$CONFIG"
