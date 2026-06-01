#!/bin/bash
# lark-agent-bridge single-config stopper.
#
# Usage:
#   ./stop.sh                 # stop this checkout's listener and lark-cli event bus
#   ./stop.sh --no-event-bus  # stop only this checkout's listener
#   ./stop.sh --no-app-server # do not clean up bridge-managed codex app-server/MCP helpers
#   ./stop.sh --force         # send SIGKILL if the listener ignores SIGTERM
#   ./stop.sh --dry-run       # show what would be stopped
#   ./stop.sh --status        # show matching listener and lark-cli event status

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG="$PROJECT_DIR/config.toml"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ -n "${LARK_AGENT_BRIDGE_PYTHON:-}" ]; then
  PYTHON_BIN="$LARK_AGENT_BRIDGE_PYTHON"
elif [ -x "$VENV_PYTHON" ]; then
  PYTHON_BIN="$VENV_PYTHON"
else
  PYTHON_BIN="python3"
fi
STOP_EVENT_BUS=1
STOP_APP_SERVER_HELPERS=1
DRY_RUN=0
FORCE=0
STATUS_ONLY=0
TIMEOUT_SECONDS="${LARK_AGENT_BRIDGE_STOP_TIMEOUT:-10}"

usage() {
  cat <<'EOF'
Usage:
  ./stop.sh [--no-event-bus] [--no-app-server] [--force] [--dry-run] [--status]

Options:
  --no-event-bus  Do not run `lark-cli event stop --json --force`.
  --no-app-server Do not clean up bridge-managed `codex app-server` / CodeGraph MCP helpers.
  --force         Send SIGKILL if the bridge listener ignores SIGTERM.
  --dry-run       Print the processes/commands that would be stopped.
  --status        Show matching listener and lark-cli event status, then exit.
  -h, --help      Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-event-bus)
      STOP_EVENT_BUS=0
      ;;
    --no-app-server)
      STOP_APP_SERVER_HELPERS=0
      ;;
    --force)
      FORCE=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --status|status)
      STATUS_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

find_bridge_pids() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk -v config="$CONFIG" '
      /python/ && /-m lark_agent_bridge listen/ && index($0, config) > 0 && !/awk/ {print $1}
    '
}

runtime_source_roots() {
  "$PYTHON_BIN" - "$CONFIG" <<'PY' 2>/dev/null || true
import sys
import tomllib
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser().resolve()
base_dir = config_path.parent
try:
    data = tomllib.load(config_path.open("rb"))
except Exception:
    data = {}

def resolve(value: object) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return path.resolve()
    except OSError:
        return path

roots: list[Path] = []
source = data.get("source_investigation") if isinstance(data.get("source_investigation"), dict) else {}
for value in source.get("repo_roots") or []:
    root = resolve(value)
    if root.exists() and root not in roots:
        roots.append(root)
for value in (data.get("workspace_root"), data.get("guideengine_repo")):
    if value:
        root = resolve(value)
        if root.exists() and root not in roots:
            roots.append(root)
for root in roots:
    print(root)
PY
}

process_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

path_is_runtime_root() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  local root
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    if [ "$candidate" = "$root" ]; then
      return 0
    fi
  done < <(runtime_source_roots)
  return 1
}

find_codex_app_server_pids() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk '/codex/ && /app-server/ && !/awk/ {print $1}' \
    | while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        cwd="$(process_cwd "$pid" || true)"
        if path_is_runtime_root "$cwd"; then
          echo "$pid"
        fi
      done
}

find_codegraph_mcp_pids() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk -v python_bin="$PYTHON_BIN" -v project="$PROJECT_DIR" '
      /-m lark_agent_bridge\.mcp_codegraph_server/ && (index($0, python_bin) > 0 || index($0, project) > 0) && !/awk/ {print $1}
    '
}

find_app_server_helper_pids() {
  {
    find_codex_app_server_pids
    find_codegraph_mcp_pids
  } | awk 'NF && !seen[$1]++ {print $1}'
}

show_status() {
  local pids=()
  while IFS= read -r pid; do
    [ -n "$pid" ] && pids+=("$pid")
  done < <(find_bridge_pids)
  if [ "${#pids[@]}" -eq 0 ]; then
    echo "Bridge listener: not running for $CONFIG"
  else
    echo "Bridge listener PIDs: ${pids[*]}"
  fi
  pids=()
  while IFS= read -r pid; do
    [ -n "$pid" ] && pids+=("$pid")
  done < <(find_app_server_helper_pids)
  if [ "${#pids[@]}" -eq 0 ]; then
    echo "Codex app-server/MCP helpers: not running for this checkout"
  else
    echo "Codex app-server/MCP helper PIDs: ${pids[*]}"
  fi
  if command -v lark-cli >/dev/null 2>&1; then
    echo "lark-cli event status:"
    lark-cli event status --json || true
  else
    echo "lark-cli: not found"
  fi
}

wait_for_exit() {
  local pid="$1"
  local elapsed=0
  while kill -0 "$pid" >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 0
}

stop_bridge() {
  local pids=() still_running=()
  while IFS= read -r pid; do
    [ -n "$pid" ] && pids+=("$pid")
  done < <(find_bridge_pids)
  if [ "${#pids[@]}" -eq 0 ]; then
    echo "Bridge listener: not running for $CONFIG"
    return 0
  fi

  echo "Stopping bridge listener PIDs: ${pids[*]}"
  for pid in "${pids[@]}"; do
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "DRY-RUN kill -TERM $pid"
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi

  for pid in "${pids[@]}"; do
    if ! wait_for_exit "$pid"; then
      still_running+=("$pid")
    fi
  done

  if [ "${#still_running[@]}" -eq 0 ]; then
    echo "Bridge listener: stopped"
    return 0
  fi

  if [ "$FORCE" -ne 1 ]; then
    echo "Bridge listener still running after ${TIMEOUT_SECONDS}s: ${still_running[*]}" >&2
    echo "Retry with ./stop.sh --force if you want to send SIGKILL." >&2
    return 1
  fi

  echo "Force stopping bridge listener PIDs: ${still_running[*]}"
  for pid in "${still_running[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
  done
}

stop_app_server_helpers() {
  if [ "$STOP_APP_SERVER_HELPERS" -ne 1 ]; then
    echo "Codex app-server/MCP helpers: skipped (--no-app-server)"
    return 0
  fi

  local pids=() still_running=()
  while IFS= read -r pid; do
    [ -n "$pid" ] && pids+=("$pid")
  done < <(find_app_server_helper_pids)
  if [ "${#pids[@]}" -eq 0 ]; then
    echo "Codex app-server/MCP helpers: not running for this checkout"
    return 0
  fi

  echo "Stopping Codex app-server/MCP helper PIDs: ${pids[*]}"
  for pid in "${pids[@]}"; do
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "DRY-RUN kill -TERM $pid"
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi

  for pid in "${pids[@]}"; do
    if ! wait_for_exit "$pid"; then
      still_running+=("$pid")
    fi
  done

  if [ "${#still_running[@]}" -eq 0 ]; then
    echo "Codex app-server/MCP helpers: stopped"
    return 0
  fi

  if [ "$FORCE" -ne 1 ]; then
    echo "Codex app-server/MCP helpers still running after ${TIMEOUT_SECONDS}s: ${still_running[*]}" >&2
    echo "Retry with ./stop.sh --force if you want to send SIGKILL." >&2
    return 1
  fi

  echo "Force stopping Codex app-server/MCP helper PIDs: ${still_running[*]}"
  for pid in "${still_running[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
  done
}

stop_event_bus() {
  if [ "$STOP_EVENT_BUS" -ne 1 ]; then
    echo "lark-cli event bus: skipped (--no-event-bus)"
    return 0
  fi
  if ! command -v lark-cli >/dev/null 2>&1; then
    echo "lark-cli event bus: lark-cli not found, skipped"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN lark-cli event stop --json --force"
    return 0
  fi
  echo "Stopping lark-cli event bus..."
  if ! lark-cli event stop --json --force; then
    echo "lark-cli event bus stop failed" >&2
    return 1
  fi
}

cd "$PROJECT_DIR"

if [ "$STATUS_ONLY" -eq 1 ]; then
  show_status
  exit 0
fi

stop_bridge
stop_app_server_helpers
stop_event_bus
