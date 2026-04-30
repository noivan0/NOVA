# Adding a Custom Provider

NOVA is designed for extension. You can add a custom LLM provider,
publisher, or notifier in about 30 lines of Python.

---

## Custom LLM Provider

### 1. Create your provider class

```python
# nova/providers/llm.py (add your class here, or import from a separate file)

from nova.providers.llm import LLMProvider
from nova.core.config import LLMConfig

class MyLLMProvider(LLMProvider):
    """
    Replace this with your actual LLM API calls.
    """

    def __init__(self, cfg: LLMConfig):
        self.api_key = cfg.api_key
        self.model = cfg.model
        self.base_url = cfg.base_url or "https://api.example.com/v1"
        self.max_tokens = cfg.max_tokens

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        """
        Send a prompt and return the text response.

        Args:
            prompt: User message / task
            system: System prompt (persona, instructions)
            timeout: Request timeout in seconds

        Returns:
            Text response from the model
        """
        import json, urllib.request

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
```

### 2. Register the provider

Open `nova/providers/llm.py` and find `get_llm_provider()`:

```python
def get_llm_provider(cfg: LLMConfig) -> LLMProvider:
    if cfg.provider == "openai":
        return OpenAIProvider(cfg)
    elif cfg.provider == "anthropic":
        return AnthropicProvider(cfg)
    elif cfg.provider == "ollama":
        return OllamaProvider(cfg)
    elif cfg.provider == "custom":
        return OpenAIProvider(cfg)   # reuses OpenAI-compatible logic
    elif cfg.provider == "echo":
        return EchoProvider(cfg)

    # Add your provider:
    elif cfg.provider == "my-provider":
        return MyLLMProvider(cfg)

    raise ValueError(f"Unknown LLM provider: {cfg.provider!r}")
```

### 3. Configure

```bash
NOVA_LLM_PROVIDER=my-provider
NOVA_LLM_MODEL=my-model
NOVA_LLM_API_KEY=my-key
NOVA_LLM_BASE_URL=https://api.example.com/v1
```

### 4. Test

```bash
NOVA_LLM_PROVIDER=my-provider nova run research --context topic="test" --dry-run
NOVA_LLM_PROVIDER=my-provider nova run research --context topic="AI basics"
```

---

## Custom Publisher

### 1. Create your publisher class

```python
from nova.providers.publisher import Publisher
from nova.core.config import PublisherConfig
from typing import List, Optional

class MyPublisher(Publisher):

    def __init__(self, cfg: PublisherConfig):
        self.api_key = cfg.api_key
        self.base_url = cfg.base_url

    def publish(
        self,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Publish the content and return the public URL (or None on failure).

        NOVA logs the returned URL in the evolution log.
        Return None if the URL is not available.
        """
        import json, urllib.request

        payload = json.dumps({
            "title": title,
            "body": content,
            "tags": tags or [],
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/api/posts",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("url")
        except Exception as e:
            print(f"[publisher/my-publisher] Failed: {e}")
            return None
```

### 2. Register the publisher

Open `nova/providers/publisher.py` and find `get_publisher()`:

```python
def get_publisher(cfg: PublisherConfig) -> Publisher:
    ...
    # Add your publisher:
    elif cfg.provider == "my-publisher":
        return MyPublisher(cfg)
    ...
```

### 3. Configure

```bash
NOVA_PUBLISHER_PROVIDER=my-publisher
NOVA_PUBLISHER_BASE_URL=https://my-cms.example.com
NOVA_PUBLISHER_API_KEY=my-key
```

---

## Custom Notifier

```python
from nova.providers.notifier import Notifier
from nova.core.config import NotifierConfig

class MyNotifier(Notifier):

    def __init__(self, cfg: NotifierConfig):
        self.webhook_url = cfg.webhook_url

    def send(self, message: str) -> bool:
        """
        Send a notification. Return True on success, False on failure.
        """
        import json, urllib.request

        payload = json.dumps({"text": message}).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            print(f"[notifier/my-notifier] Failed: {e}")
            return False
```

Register in `nova/providers/notifier.py` → `get_notifier()`, same pattern as above.

---

## Contributing your provider

If your provider might be useful to others, consider contributing it to NOVA:

1. Add your provider class to `nova/providers/llm.py`, `notifier.py`, or `publisher.py`
2. Add a test in `tests/unit/`
3. Document it in `docs/guides/providers.md`
4. Open a pull request

See [CONTRIBUTING](../../README.md#contributing) for the full process.
