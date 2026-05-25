#!/usr/bin/env bash
set -euo pipefail

# Manual runner only. This script does not install launchd jobs or enable
# login/startup auto-run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
cd "${REPO_ROOT}"

USER_HOME="${HOME:-/Users/$(id -un)}"
export HOME="${USER_HOME}"
export PATH="${HOME}/.local/share/mise/installs/node/lts/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

CONFIG_PATH="${LARK_AGENT_BRIDGE_CONFIG:-${REPO_ROOT}/config.toml}"
PYTHON_BIN="${PYTHON_BIN:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/run-listener.sh [listen|check|dry-run] [extra args...]

Modes:
  listen   Run the bridge listener in the foreground. Press Ctrl-C to stop.
  check    Print environment/config/health diagnostics and exit.
  dry-run  Validate the listen command path without consuming events.

Environment:
  LARK_AGENT_BRIDGE_CONFIG  Override config path. Default: ./config.toml
  PYTHON_BIN                Override Python executable. Default: python3.11

This script is for manual execution only. It does not configure launchctl,
login items, or startup persistence.
EOF
}

find_python() {
  if [[ -n "${PYTHON_BIN}" ]]; then
    printf '%s\n' "${PYTHON_BIN}"
    return
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi
  if [[ -x /opt/homebrew/bin/python3.11 ]]; then
    printf '%s\n' /opt/homebrew/bin/python3.11
    return
  fi
  echo "python3.11 not found. Set PYTHON_BIN=/path/to/python3.11." >&2
  exit 127
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

MODE="${1:-listen}"
if [[ $# -gt 0 ]]; then
  shift
fi

PYTHON_BIN="$(find_python)"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config not found: ${CONFIG_PATH}" >&2
  echo "Create local config.toml from config/config.example.toml before starting the listener." >&2
  exit 2
fi

case "${MODE}" in
  listen|run)
    exec "${PYTHON_BIN}" -m lark_agent_bridge listen --config "${CONFIG_PATH}" "$@"
    ;;
  check|status)
    exec "${PYTHON_BIN}" -m lark_agent_bridge check --config "${CONFIG_PATH}" "$@"
    ;;
  dry-run)
    exec "${PYTHON_BIN}" -m lark_agent_bridge listen --config "${CONFIG_PATH}" --dry-run "$@"
    ;;
  *)
    echo "Unknown mode: ${MODE}" >&2
    usage >&2
    exit 2
    ;;
esac
