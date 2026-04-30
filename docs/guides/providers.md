# Provider Setup Guide

This guide covers setup for all LLM, notifier, and publisher providers.

---

## LLM Providers

### OpenAI

**Models:** `gpt-4.1`, `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini`, `gpt-4.1-mini`

```bash
pip install "nova-orchestrator[openai]"
# or: pip install -e ".[openai]"
```

`.env`:
```bash
NOVA_LLM_PROVIDER=openai
NOVA_LLM_MODEL=gpt-4o
NOVA_LLM_API_KEY=sk-...
```

**Reasoning models** (`o3`, `o4-mini`): NOVA automatically omits `temperature` and uses
`max_completion_tokens` instead of `max_tokens` — no special config needed.

---

### Anthropic Claude

**Models:** `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-6`

```bash
pip install "nova-orchestrator[anthropic]"
```

`.env`:
```bash
NOVA_LLM_PROVIDER=anthropic
NOVA_LLM_MODEL=claude-sonnet-4-6
NOVA_LLM_API_KEY=sk-ant-...
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
NOVA_LLM_API_KEY=my-api-key
```

**Azure OpenAI example:**
```bash
NOVA_LLM_PROVIDER=custom
NOVA_LLM_MODEL=gpt-4o
NOVA_LLM_BASE_URL=https://my-resource.openai.azure.com/openai/deployments/my-deployment/
NOVA_LLM_API_KEY=my-azure-key
```

---

### Echo (testing, no API key)

Returns the prompt back as the response. Useful for testing harness structure without API calls.

```bash
NOVA_LLM_PROVIDER=echo
```

All 14 NOVA tests use the echo provider — no API key required.

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
NOVA_NOTIFIER_TOKEN=1234567890:ABCdef...
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
{"text": "NOVA harness 'blog-pipeline' completed successfully. Quality: 88"}
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
NOVA_PUBLISHER_API_KEY=myusername:xxxx xxxx xxxx xxxx xxxx xxxx
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
NOVA_PUBLISHER_API_KEY=6478f...abc:1a2b3c...
```

---

### Blogger

Uses Google Blogger API v3 with a Google OAuth2 access token.

**Setup:**
1. Google Cloud Console → Create project → Enable Blogger API
2. Create OAuth2 credentials
3. Get your access token (use `google-auth` library or OAuth playground)
4. Get your Blog ID from: Blogger Dashboard → Settings → Blog ID

```bash
NOVA_PUBLISHER_PROVIDER=blogger
NOVA_PUBLISHER_API_KEY=ya29.a0AfH...    # OAuth2 access token
NOVA_PUBLISHER_BLOG_ID=1234567890
```

Note: OAuth2 access tokens expire in 1 hour. For long-running scheduled workflows,
implement token refresh or use a service account.

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
url = p.publish(title='NOVA test', content='<p>Test post from NOVA</p>')
print('URL:', url)
"
```
