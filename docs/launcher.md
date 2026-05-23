# Multi-Config Launcher Guide

## Overview

The `run.sh` launcher simplifies starting the lark-agent-bridge service with different AI provider configurations.

## Available Presets

### 1. CC-Switch Preset (Recommended)

**Config**: `config.cc-switch.toml`

```bash
./run.sh cc-switch
```

**What it does:**
- Routes all AI requests to local `cc-switch` proxy at `127.0.0.1:15721`
- `cc-switch` manages multiple LLM providers (Claude, Codex, DeepSeek, etc.)
- **Zero configuration**: no API keys needed
- Provider can be switched dynamically via cc-switch UI
- Supports fallback chains configured in cc-switch

**Requirements:**
- cc-switch service running: `lsof -i :15721` should show LISTEN
- Valid Feishu bot credentials in config

**Use case:**
- Development / testing with multiple providers
- Flexibility: switch providers without restarting
- Cost control: route expensive queries to cheaper providers

**Status of cc-switch:**
```bash
# Check if cc-switch is running
lsof -i :15721

# Typical output if running:
# cc-switch  6715 zhuyl   21u  IPv4 0xabcdef  0t0  TCP 127.0.0.1:15721 (LISTEN)
```

---

### 2. OpenAI Direct API

**Config**: `config.openai.toml`

```bash
./run.sh openai
```

**What it does:**
- Routes all AI requests directly to OpenAI API
- No local proxy or cc-switch needed
- Requires valid OpenAI API key

**Requirements:**
- OpenAI API key: `export LARK_AGENT_BRIDGE_AI_API_KEY=sk-...`
- Valid Feishu bot credentials in config

**API key setup (choose one):**

```bash
# Option A: Environment variable (preferred for security)
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-proj-..."
./run.sh openai

# Option B: Edit config.openai.toml directly (not recommended)
# config.openai.toml
# [ai_provider]
# api_key = "sk-proj-..."  # ⚠️ Never commit this to Git!
```

**Use case:**
- Production deployments with stable OpenAI pricing
- No dependency on local infrastructure
- Cost predictable with GPT-4 / GPT-4.1 mini

**Cost example (approximate):**
- Intent classification: $0.0001-0.0005 per call
- Bug summary: $0.001-0.005 per call

---

## Usage Examples

### Default Launch (cc-switch)

```bash
cd /path/to/lark-agent-bridge
./run.sh

# Output:
# 🔌 Starting lark-agent-bridge with cc-switch preset...
#    (cc-switch proxy at 127.0.0.1:15721 must be running)
#    Check: lsof -i :15721
# 📝 Config: /path/to/config.cc-switch.toml
# 📡 Reports: http://localhost:18888
# Press Ctrl+C to stop
```

### Launch with OpenAI

```bash
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-..."
./run.sh openai

# Output:
# 🔑 Starting lark-agent-bridge with OpenAI direct API...
#    Requires: LARK_AGENT_BRIDGE_AI_API_KEY or config.openai.toml api_key
# 📝 Config: /path/to/config.openai.toml
# 📡 Reports: http://localhost:18888
# Press Ctrl+C to stop
```

### Show Help

```bash
./run.sh invalid

# Output:
# Usage: ./run.sh {cc-switch|openai}
# 
# Presets:
#   cc-switch  - Use local cc-switch proxy (127.0.0.1:15721)
#   openai     - Direct OpenAI API (requires API key)
# 
# Other cc-switch presets available in code:
#   xiaomi-tp, xiaomi-sk, yybb, yybb-codex, deepseek
```

---

## Advanced: Custom Config

If you need a custom configuration beyond the two presets, use the CLI directly:

```bash
python3 -m lark_agent_bridge listen --config config.custom.toml
```

Or with environment variable override:

```bash
export LARK_AGENT_BRIDGE_AI_PRESET="deepseek"
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-..."
python3 -m lark_agent_bridge listen --config config.custom.toml
```

---

## Troubleshooting

### cc-switch preset fails to connect

**Error**: `Connection refused at 127.0.0.1:15721`

**Solution:**
```bash
# 1. Check if cc-switch is running
lsof -i :15721

# 2. If not running, start it
cc-switch  # or your local cc-switch launcher

# 3. Verify it responds
curl http://127.0.0.1:15721/health  # if it has a health endpoint
```

### OpenAI preset fails with invalid API key

**Error**: `Invalid API key provided`

**Solution:**
```bash
# 1. Verify API key is set
echo $LARK_AGENT_BRIDGE_AI_API_KEY

# 2. If empty, set it
export LARK_AGENT_BRIDGE_AI_API_KEY="sk-proj-..."

# 3. Retry
./run.sh openai
```

### Script permission denied

**Error**: `Permission denied: ./run.sh`

**Solution:**
```bash
chmod +x run.sh
./run.sh cc-switch
```

### Reports server not accessible

**Verify** port 18888 is not blocked:
```bash
lsof -i :18888
# Should show LISTEN on 127.0.0.1:18888
```

**Access reports:**
```
http://127.0.0.1:18888/reports
```

---

## Configuration Files Reference

### config.cc-switch.toml

```toml
[ai_provider]
enabled = true
preset = "cc-switch"    # Zero-config: cc-switch handles everything
```

**Advantages:**
- Simplest config
- Supports multiple providers without restarting
- Provider switching via UI

**Limitations:**
- Requires cc-switch running
- No direct control over model selection (managed by cc-switch)

---

### config.openai.toml

```toml
[ai_provider]
enabled = true
preset = "openai"
api_key = ""            # Set via env var (preferred) or here (not recommended)

[ai_provider]
intent_timeout_seconds = 30
summary_timeout_seconds = 120
summary_max_tokens = 4096
```

**Advantages:**
- No local dependencies
- Full model control
- Predictable pricing with OpenAI

**Limitations:**
- Requires API key management
- Outbound internet required

---

## Performance Comparison

| Metric | cc-switch | OpenAI Direct |
|--------|-----------|---------------|
| **Setup** | Complex (needs cc-switch running) | Simple (export API key) |
| **Latency** | ~3-10s (HTTP to proxy) | ~3-10s (HTTP to OpenAI) |
| **Provider Switching** | Dynamic (UI) | Requires restart |
| **Cost** | Depends on cc-switch routing | Fixed OpenAI rates |
| **Availability** | Depends on cc-switch | Depends on OpenAI API |

---

## Environment Variables

All presets support these environment variables:

| Variable | Purpose | Example |
|----------|---------|---------|
| `LARK_AGENT_BRIDGE_AI_API_KEY` | API key for OpenAI / presets | `sk-...` |
| `LARK_AGENT_BRIDGE_AI_PRESET` | Override preset | `openai`, `cc-switch` |
| `LARK_AGENT_BRIDGE_BOT_NAME` | Bot name for Feishu | `My Feishu Bot` |
| `LARK_AGENT_BRIDGE_DRY_RUN` | Dry-run mode | `1` (enabled) |

See [configuration.md](configuration.md) for full reference.

---

## Next Steps

1. **Choose a preset** — Start with `./run.sh cc-switch` if cc-switch is running, otherwise `./run.sh openai`
2. **Configure Feishu credentials** — Update config file with bot_id, bot_secret, webhook_secret
3. **Test it** — Send a test message to the Feishu bot
4. **Monitor reports** — Check http://127.0.0.1:18888/reports
5. **Adjust settings** — Tune timeouts, token limits, or provider preferences as needed

See [configuration.md](configuration.md) for detailed config options.
