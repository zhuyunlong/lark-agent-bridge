#!/usr/bin/env bash
# One-shot runtime environment check for lark-agent-bridge.

set -u

PROBE_AI=0

for arg in "$@"; do
  case "$arg" in
    --probe-ai)
      PROBE_AI=1
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/check-runtime-env.sh [--probe-ai]

Checks:
  - current shell proxy state: proxy_on / proxy_off / proxy_partial
  - macOS effective proxy state
  - lark-cli event bus and consumer status
  - event _bus and bridge process proxy environments
  - bridge process LARK_AGENT_BRIDGE_AI_API_KEY inheritance (masked)
  - Meegle auth status

Options:
  --probe-ai   Probe the effective [ai_provider] from config.toml with the bridge process key.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS %s\n' "$*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); printf 'WARN %s\n' "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL %s\n' "$*"; }
info() { printf 'INFO %s\n' "$*"; }

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

proxy_vars_present() {
  env | grep -E '^(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|all_proxy)=' || true
}

classify_proxy_blob() {
  local blob="$1"
  local http https upper_http upper_https all_proxy lower_all count
  http="$(printf '%s\n' "$blob" | sed -n 's/^http_proxy=//p' | head -n 1)"
  https="$(printf '%s\n' "$blob" | sed -n 's/^https_proxy=//p' | head -n 1)"
  upper_http="$(printf '%s\n' "$blob" | sed -n 's/^HTTP_PROXY=//p' | head -n 1)"
  upper_https="$(printf '%s\n' "$blob" | sed -n 's/^HTTPS_PROXY=//p' | head -n 1)"
  lower_all="$(printf '%s\n' "$blob" | sed -n 's/^all_proxy=//p' | head -n 1)"
  all_proxy="$(printf '%s\n' "$blob" | sed -n 's/^ALL_PROXY=//p' | head -n 1)"
  count="$(printf '%s\n' "$blob" | grep -E '^(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|all_proxy)=' | wc -l | tr -d ' ')"

  if [ "${count:-0}" = "0" ]; then
    printf 'proxy_off'
    return
  fi
  if [ -n "$http" ] && [ "$http" = "$https" ] && [ "$http" = "$upper_http" ] && [ "$http" = "$upper_https" ] && [ -z "$all_proxy" ] && [ -z "$lower_all" ]; then
    printf 'proxy_on'
    return
  fi
  printf 'proxy_partial'
}

mask_value() {
  local value="$1"
  if [ -z "$value" ]; then
    printf '<unset>'
    return
  fi
  if has_cmd shasum; then
    local digest
    digest="$(printf '%s' "$value" | shasum -a 256 | awk '{print $1}' | cut -c1-12)"
    printf '<set len=%s sha256=%s>' "${#value}" "$digest"
  else
    printf '<set len=%s>' "${#value}"
  fi
}

pid_env_lines() {
  local pid="$1"
  ps eww -p "$pid" 2>/dev/null | tr ' ' '\n' || true
}

pid_proxy_state() {
  local pid="$1"
  local lines state
  lines="$(pid_env_lines "$pid" | grep -E '^(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|all_proxy)=' || true)"
  state="$(classify_proxy_blob "$lines")"
  printf '%s' "$state"
}

find_bridge_pid() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk '/python.*-m lark_agent_bridge listen/ && !/awk/ {print $1; exit}'
}

find_event_bus_pid() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk '/lark-cli event _bus/ && !/awk/ {print $1; exit}'
}

find_event_consume_pid() {
  ps axww -o pid= -o command= 2>/dev/null \
    | awk '/lark-cli event consume im\.message\.receive_v1/ && !/awk/ {print $1; exit}'
}

section() {
  printf '\n== %s ==\n' "$1"
}

section "Shell Proxy"
shell_proxy_lines="$(proxy_vars_present)"
shell_proxy_state="$(classify_proxy_blob "$shell_proxy_lines")"
case "$shell_proxy_state" in
  proxy_on)
    pass "current_shell_proxy=proxy_on value=${http_proxy:-${HTTP_PROXY:-}}"
    ;;
  proxy_off)
    pass "current_shell_proxy=proxy_off"
    ;;
  *)
    warn "current_shell_proxy=proxy_partial set_vars=$(printf '%s' "$shell_proxy_lines" | cut -d= -f1 | paste -sd, -)"
    ;;
esac

section "macOS Proxy"
if has_cmd scutil; then
  proxy_summary="$(scutil --proxy 2>/dev/null | awk '/HTTPEnable|HTTPSEnable|SOCKSEnable|ProxyAutoConfigEnable/ {gsub(/[[:space:]]+/, "", $0); print}' | paste -sd' ' -)"
  if printf '%s' "$proxy_summary" | grep -q 'HTTPEnable:1\|HTTPSEnable:1\|SOCKSEnable:1\|ProxyAutoConfigEnable:1'; then
    warn "macos_proxy=enabled $proxy_summary"
  else
    pass "macos_proxy=disabled $proxy_summary"
  fi
else
  warn "macos_proxy=unknown scutil_not_found"
fi

section "lark-cli Event"
if has_cmd lark-cli; then
  event_status="$(lark-cli event status --json 2>&1)"
  event_status_code=$?
  if [ "$event_status_code" -eq 0 ]; then
    info "lark_event_status=$event_status"
    if printf '%s' "$event_status" | grep -q '"running": true'; then
      pass "lark_event_bus=running"
    else
      warn "lark_event_bus=not_running"
    fi
    if printf '%s' "$event_status" | grep -q '"event_key": "im.message.receive_v1"'; then
      pass "lark_event_consumer=im.message.receive_v1"
    else
      warn "lark_event_consumer=missing_im.message.receive_v1"
    fi
  else
    fail "lark_event_status_failed exit=$event_status_code output=$event_status"
  fi
else
  fail "lark-cli=missing"
fi

event_bus_pid="$(find_event_bus_pid)"
event_consume_pid="$(find_event_consume_pid)"
bridge_pid="$(find_bridge_pid)"

if [ -n "$event_bus_pid" ]; then
  event_bus_proxy="$(pid_proxy_state "$event_bus_pid")"
  case "$event_bus_proxy" in
    proxy_on|proxy_off) pass "event_bus_proxy=$event_bus_proxy pid=$event_bus_pid" ;;
    *) warn "event_bus_proxy=$event_bus_proxy pid=$event_bus_pid" ;;
  esac
else
  warn "event_bus_process=not_found"
fi

if [ -n "$event_consume_pid" ]; then
  pass "event_consumer_process=running pid=$event_consume_pid"
else
  warn "event_consumer_process=not_found"
fi

section "Bridge"
if [ -n "$bridge_pid" ]; then
  bridge_proxy="$(pid_proxy_state "$bridge_pid")"
  case "$bridge_proxy" in
    proxy_on|proxy_off) pass "bridge_proxy=$bridge_proxy pid=$bridge_pid" ;;
    *) warn "bridge_proxy=$bridge_proxy pid=$bridge_pid" ;;
  esac
  bridge_key="$(pid_env_lines "$bridge_pid" | sed -n 's/^LARK_AGENT_BRIDGE_AI_API_KEY=//p' | head -n 1)"
  if [ -n "$bridge_key" ]; then
    pass "bridge_ai_key=$(mask_value "$bridge_key")"
  else
    warn "bridge_ai_key=<unset>"
  fi
else
  fail "bridge_process=not_found"
fi

if [ "$shell_proxy_state" != "proxy_partial" ] && [ -n "${event_bus_pid:-}" ]; then
  if [ "$shell_proxy_state" = "$event_bus_proxy" ]; then
    pass "proxy_alignment=shell_matches_event_bus state=$shell_proxy_state"
  else
    warn "proxy_alignment=shell_${shell_proxy_state}_event_bus_${event_bus_proxy}; run lark_bus_reset then ./run.sh after switching proxy mode"
  fi
fi

section "Meegle"
if has_cmd meegle; then
  meegle_status="$(meegle auth status 2>&1)"
  meegle_code=$?
  if [ "$meegle_code" -eq 0 ] && printf '%s' "$meegle_status" | grep -q '"authenticated": true'; then
    pass "meegle_auth=valid $meegle_status"
  else
    fail "meegle_auth=invalid exit=$meegle_code output=$meegle_status"
  fi
else
  fail "meegle=missing"
fi

section "AI Probe"
if [ "$PROBE_AI" -eq 1 ]; then
  if [ -z "${bridge_key:-}" ]; then
    fail "ai_probe=skipped bridge_ai_key_unset"
  elif ! has_cmd python3; then
    fail "ai_probe=skipped python3_missing"
  else
    probe_output="$(
      BRIDGE_AI_KEY="$bridge_key" PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import json
import os
import urllib.error
import urllib.request

from lark_agent_bridge.config import load_config


def endpoint_for(base_url: str, api_format: str) -> str:
    url = base_url.rstrip("/")
    if api_format == "anthropic":
        return f"{url}/messages" if url.endswith("/v1") else f"{url}/v1/messages"
    return f"{url}/chat/completions" if url.endswith("/v1") else f"{url}/v1/chat/completions"


key = os.environ.get("BRIDGE_AI_KEY", "")
config = load_config("config.toml")
provider = config.ai_provider
api_format = provider.api_format or ("anthropic" if "/anthropic" in provider.base_url or "mimo" in provider.base_url or "yybb" in provider.base_url else "openai")
base_url = provider.base_url
model = provider.fast_model or provider.primary_model
if not provider.enabled:
    print("config_error ai_provider_disabled")
    raise SystemExit(1)
if not base_url or not model:
    print("config_error ai_provider_missing_base_url_or_model")
    raise SystemExit(1)
url = endpoint_for(base_url, api_format)
if api_format == "anthropic":
    payload = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
else:
    payload = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        usage = body.get("usage") or {}
        if api_format == "anthropic":
            prompt_tokens = usage.get("input_tokens", "?")
            completion_tokens = usage.get("output_tokens", "?")
        else:
            prompt_tokens = usage.get("prompt_tokens", "?")
            completion_tokens = usage.get("completion_tokens", "?")
        print(f"ok provider={provider.preset or 'custom'} format={api_format} status={resp.status} model={body.get('model', model)} prompt_tokens={prompt_tokens} completion_tokens={completion_tokens}")
except urllib.error.HTTPError as exc:
    print(f"http_error provider={provider.preset or 'custom'} format={api_format} status={exc.code} body={exc.read().decode('utf-8', errors='replace')[:300]}")
    raise SystemExit(1)
except Exception as exc:
    print(f"request_error provider={provider.preset or 'custom'} format={api_format} {type(exc).__name__}: {exc}")
    raise SystemExit(1)
PY
    )"
    probe_code=$?
    if [ "$probe_code" -eq 0 ]; then
      pass "ai_probe=$probe_output"
    else
      fail "ai_probe_failed output=$probe_output"
    fi
  fi
else
  info "ai_probe=skipped use --probe-ai"
fi

printf '\n== Summary ==\n'
printf 'PASS=%s WARN=%s FAIL=%s\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
