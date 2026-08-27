# Provider Setup Guide

This guide covers setup for all LLM, notifier, and publisher providers.

---

## LLM Providers

### OpenAI

**Models:** `gpt-5.5`, `gpt-5`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `o3`, `o4-mini`

```bash
pip install "nova-orchestrator[openai]"
# or: pip install -e ".[openai]"
```

`.env`:
```bash
NOVA_LLM_PROVIDER=openai
NOVA_LLM_MODEL=gpt-4o
NOVA_LLM_API_KEY=***
```

**Reasoning models** (`o3`, `o4-mini`): NOVA automatically omits `temperature` and uses
`max_completion_tokens` instead of `max_tokens` — no special config needed.

---

### Anthropic Claude

**Models:** `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`

```bash
pip install "nova-orchestrator[anthropic]"
```

`.env`:
```bash
NOVA_LLM_PROVIDER=anthropic
NOVA_LLM_MODEL=claude-sonnet-4-6
NOVA_LLM_API_KEY=***
```

---

### Ollama (local inference, free)

Run any model locally — no API key, no cost.

1. Install Ollama: https://ollama.com
2. Pull a model:

```bash
ollama pull llama3.3          # Meta Llama 3.3 70B (recommended)
ollama pull gemma3            # Google Gemma 3
ollama pull qwen3             # Qwen 3
ollama pull mistral           # Mistral 7B (fast, lightweight)
ollama pull deepseek-r1       # DeepSeek R1 (reasoning)
```

3. Configure NOVA:

```bash
pip install "nova-orchestrator[ollama]"
```

`.env`:
```bash
NOVA_LLM_PROVIDER=ollama
NOVA_LLM_MODEL=llama3.3
NOVA_LLM_BASE_URL=http://localhost:11434   # default; omit if Ollama runs locally
```

---

### Custom OpenAI-compatible endpoint

Works with: Azure OpenAI, LM Studio, vLLM, LocalAI, any OpenAI-compatible gateway.

`.env`:
```bash
NOVA_LLM_PROVIDER=custom
NOVA_LLM_MODEL=my-model-name
NOVA_LLM_BASE_URL=https://my-gateway.example.com/v1
NOVA_LLM_API_KEY=***
```

**Azure OpenAI example:**
```bash
NOVA_LLM_PROVIDER=custom
NOVA_LLM_MODEL=gpt-4o
NOVA_LLM_BASE_URL=https://my-resource.openai.azure.com/openai/deployments/my-deployment/
NOVA_LLM_API_KEY=***
```

---

### Echo (testing, no API key)

Returns the prompt back as the response. Useful for testing harness structure without API calls.

```bash
NOVA_LLM_PROVIDER=echo
```

All 14 NOVA tests use the echo provider — no API key required.

---

### General-purpose gateways (preset providers, 2026-08-28+)

These provider names resolve to their public API `base_url` automatically —
no need to look up or type the endpoint yourself. They all reuse the
OpenAI-compatible client under the hood (same code path as `custom`), so
existing `hmg` / `codex` / `openai` / `anthropic` / `ollama` / `echo` setups
are completely unaffected.

| Provider name | Service |
| --- | --- |
| `groq` | Groq (LPU ultra-fast inference: Llama/Mixtral/Gemma) |
| `deepseek` | DeepSeek official API |
| `mistral` | Mistral AI official API |
| `xai` | xAI Grok official API |
| `moonshot` | Moonshot Kimi official API |
| `zhipu` | Zhipu GLM official API |
| `openrouter` | OpenRouter (hundreds of models via one gateway) |
| `together` | Together AI (open-model hosting) |
| `fireworks` | Fireworks AI (open-model hosting) |
| `perplexity` | Perplexity (web-search-grounded models) |

```bash
NOVA_LLM_PROVIDER=groq
NOVA_LLM_MODEL=llama-3.3-70b-versatile
NOVA_LLM_API_KEY=***
```

Setting `NOVA_LLM_BASE_URL` explicitly always overrides the preset URL —
useful for self-hosted or region-specific mirrors of the same API.

See `nova/providers/llm.py::GATEWAY_PRESETS` for the exact URLs, and add
your own entry there (one line) to support additional gateways.

---

### Fallback chain (multi-provider, 2026-08-28+)

Chain several provider/model pairs together — if one fails (auth error,
rate limit, network issue), NOVA automatically tries the next:

```bash
NOVA_LLM_FALLBACK_CHAIN="hmg:claude-sonnet-4-6,groq:llama-3.3-70b-versatile,ollama:llama3.3"
NOVA_LLM_API_KEY=***   # shared across all chain entries (single master key)
```

```python
from nova.providers.llm import get_fallback_chain_from_env
chain = get_fallback_chain_from_env()  # None if NOVA_LLM_FALLBACK_CHAIN unset
if chain:
    output = chain.complete("your prompt")
```

This is opt-in: existing single-provider code paths (`get_llm_provider(cfg)`)
are completely unchanged. `get_fallback_chain_from_env()` returns `None`
when the environment variable is unset, so nothing changes unless you
explicitly configure a chain.

---

## Notifier Providers

### None (default)

Silent operation. No configuration needed.

```bash
NOVA_NOTIFIER_PROVIDER=none
```

---

### Telegram

Get notified when a harness completes or fails via Telegram.

**Setup:**
1. Create a bot: message `@BotFather` on Telegram → `/newbot`
2. Note the bot token
3. Add the bot to your channel/group
4. Get the chat ID: send a message, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and look for `"chat":{"id":...}`

`.env`:
```bash
NOVA_NOTIFIER_PROVIDER=telegram
NOVA_NOTIFIER_TOKEN=123456...f...
NOVA_NOTIFIER_CHAT_ID=-1001234567890
```

---

### Slack

```bash
NOVA_NOTIFIER_PROVIDER=slack
NOVA_NOTIFIER_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
```

Create a webhook: Slack App → Incoming Webhooks → Add New Webhook to Workspace.

---

### Discord

```bash
NOVA_NOTIFIER_PROVIDER=discord
NOVA_NOTIFIER_WEBHOOK_URL=https://discord.com/api/webhooks/.../.../ 
```

Create a webhook: Discord Server → Channel Settings → Integrations → Webhooks.

---

### Generic Webhook

Posts JSON to any HTTP endpoint:

```bash
NOVA_NOTIFIER_PROVIDER=webhook
NOVA_NOTIFIER_WEBHOOK_URL=https://my-server.example.com/nova-webhook
```

Payload format:
```json
{"text": "NOVA harness 'research' completed successfully. Quality: 88"}
```

---

## Publisher Providers

### None

Content stays in `workspace/`. No publishing. Default.

```bash
NOVA_PUBLISHER_PROVIDER=none
```

---

### File

Write output to a local directory. Great for static site generators (Hugo, Jekyll, Docusaurus).

```bash
NOVA_PUBLISHER_PROVIDER=file
NOVA_PUBLISHER_OUTPUT_DIR=./output   # default: ./output
```

Files are named `<title>.md` (slugified) in `output_dir`.

---

### WordPress

Uses WordPress REST API v2 with Application Passwords (no plugin required, WordPress 5.6+).

**Setup:**
1. In WordPress Admin: Users → Profile → Application Passwords → Add New
2. Note the generated password

```bash
NOVA_PUBLISHER_PROVIDER=wordpress
NOVA_PUBLISHER_BASE_URL=https://myblog.com
NOVA_PUBLISHER_API_KEY=*** xxxx xxxx xxxx xxxx xxxx
```

Posts are created as drafts by default. Modify `nova/providers/publisher.py`
to set `"status": "publish"` to auto-publish.

---

### Ghost

Uses Ghost Admin API v5 with an Admin API key.

**Setup:**
1. Ghost Admin → Settings → Integrations → Add Custom Integration
2. Note the Admin API key (format: `id:hex_secret`)

```bash
NOVA_PUBLISHER_PROVIDER=ghost
NOVA_PUBLISHER_BASE_URL=https://myblog.ghost.io
NOVA_PUBLISHER_API_KEY=6478f....c...
```

---

## Testing your configuration

```bash
# Test LLM (dry-run: validates config, no API calls)
nova run research --context topic="test" --dry-run

# Test LLM with actual call (echo provider, free)
NOVA_LLM_PROVIDER=echo nova run research --context topic="test"

# Test notifier — sends a test message
python -c "
from nova.core.config import load_config
from nova.providers.notifier import get_notifier
cfg = load_config('nova.yaml')
n = get_notifier(cfg.notifier)
print('Sent:', n.send('NOVA notifier test — this is working!'))
"

# Test publisher — publish a test file
python -c "
from nova.core.config import load_config
from nova.providers.publisher import get_publisher
cfg = load_config('nova.yaml')
p = get_publisher(cfg.publisher)
url = p.publish(title='NOVA test', content='<p>Test output from NOVA</p>')
print('URL:', url)
"
```
