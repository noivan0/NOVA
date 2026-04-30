"""
examples/custom_provider.py
-----------------------------
How to implement and register a custom LLM provider.

Use this when:
- You have a private / enterprise LLM endpoint
- You want to wrap a provider not yet supported by NOVA
- You want to add caching, logging, or custom retry logic

Usage:
    1. Copy this file and implement MyCustomLLM.complete()
    2. Register it in nova/providers/llm.py -> get_llm_provider()
    3. Set NOVA_LLM_PROVIDER=my-custom in nova.yaml or .env
"""

from nova.providers.llm import LLMProvider
from nova.core.config import LLMConfig
import json
import urllib.request


class MyCustomLLM(LLMProvider):
    """
    Example custom LLM provider for any HTTP-based inference endpoint.

    Configure in nova.yaml:
        llm:
          provider: custom
          base_url: https://my-api.example.com/v1/completions
          api_key: my-secret-key
          model: my-model-name

    Or via environment variables:
        NOVA_LLM_PROVIDER=custom
        NOVA_LLM_BASE_URL=https://my-api.example.com/v1/completions
        NOVA_LLM_API_KEY=my-secret-key
    """

    def __init__(self, cfg: LLMConfig):
        self.endpoint = cfg.base_url or "https://api.example.com/v1/completions"
        self.api_key = cfg.api_key
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        """
        Send a prompt to the custom endpoint and return the text response.

        The example below uses the OpenAI Chat Completions format.
        Adjust the payload structure to match your API.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")

        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # Adjust key path to match your API's response schema
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Custom LLM call failed: {e}") from e


# ── Registration ─────────────────────────────────────────────────────────────
# To activate this provider, add it to nova/providers/llm.py:
#
#   from examples.custom_provider import MyCustomLLM
#
#   def get_llm_provider(cfg: LLMConfig) -> LLMProvider:
#       if cfg.provider == "my-custom":
#           return MyCustomLLM(cfg)
#       ...
#
# Then set in nova.yaml or .env:
#   NOVA_LLM_PROVIDER=my-custom
# ─────────────────────────────────────────────────────────────────────────────
