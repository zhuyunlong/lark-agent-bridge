#!/usr/bin/env bash
# ---------------------------------------------------------------
# 启动 lark-agent-bridge — OpenAI 兼容模式
# 用法:
#   ./run-openai.sh                    # 默认使用 config.openai.toml 中的 preset
#   ./run-openai.sh --preset panda     # 覆盖 preset
#   ./run-openai.sh --preset yybb-codex-openai    # 走 yybb.codes 的 OpenAI 兼容网关
# ---------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ============ 在这里填入你的 API Key ============
# 方式1: 直接写在这里
export LARK_AGENT_BRIDGE_AI_API_KEY="${LARK_AGENT_BRIDGE_AI_API_KEY:-sk-your-api-key-here}"

# 方式2: 从 .env 文件加载 (如果存在)
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi
# ================================================

# 可选: 通过命令行参数覆盖 preset
PRESET_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --preset)
            PRESET_OVERRIDE="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

if [[ -n "$PRESET_OVERRIDE" ]]; then
    export LARK_AGENT_BRIDGE_AI_PRESET="$PRESET_OVERRIDE"
    echo "🔧 Preset override: $PRESET_OVERRIDE"
fi

echo "🚀 Starting lark-agent-bridge (OpenAI mode)"
echo "   Config: config.openai.toml"
echo "   AI Key: ${LARK_AGENT_BRIDGE_AI_API_KEY:0:8}..."
echo ""

# 激活虚拟环境 (如果存在)
if [[ -f .venv/bin/activate ]]; then
    source .venv/bin/activate
fi

exec python -m lark_agent_bridge --config config.openai.toml "$@"
