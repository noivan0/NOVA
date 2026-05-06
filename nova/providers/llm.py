"""
nova/providers/llm.py
---------------------
Pluggable LLM provider abstraction.

Supported providers:
  openai     — OpenAI API (GPT-4o, etc.) or any OpenAI-compatible endpoint
  anthropic  — Anthropic Claude API
  ollama     — Local Ollama inference
  custom     — Any OpenAI-compatible endpoint (set NOVA_LLM_BASE_URL)

Usage:
  from nova.providers.llm import get_llm_provider
  llm = get_llm_provider(config.llm)
  response = llm.complete("Write a blog post about Paris.")
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nova.core.config import LLMConfig


class LLMProvider(ABC):
    """Base class for all LLM providers."""

    @abstractmethod
    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        """Send a prompt and return the text completion."""
        ...

    def chat(self, messages: list, timeout: int = 120) -> str:
        """Send a list of chat messages (OpenAI format) and return the reply."""
        # Default: convert to a single prompt
        combined = "\n\n".join(
            f"[{m['role'].upper()}]\n{m['content']}" for m in messages
        )
        return self.complete(combined, timeout=timeout)


# --------------------------------------------------------------------------- #
# OpenAI / OpenAI-compatible
# --------------------------------------------------------------------------- #

class OpenAIProvider(LLMProvider):
    """
    Works with:
      - OpenAI (api.openai.com) — openai>=2.0
      - Custom/enterprise OpenAI-compatible endpoints (set base_url)
      - Azure OpenAI (set base_url to Azure endpoint)

    Reasoning models (o1, o3, o4-mini, o1-mini, o1-preview):
      temperature is not supported; use max_completion_tokens instead of max_tokens.
    """

    # Models that use the Responses API reasoning parameter set
    _REASONING_PREFIXES = ("o1", "o3", "o4")

    def __init__(self, cfg: LLMConfig):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required: pip install 'openai>=2.0'")

        api_key = cfg.api_key or "***"
        if cfg.base_url:
            self.client = OpenAI(api_key=api_key, base_url=cfg.base_url)
        else:
            self.client = OpenAI(api_key=api_key)
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature

    def _is_reasoning_model(self) -> bool:
        """Detect o1/o3/o4-series models that don't support temperature."""
        return any(self.model.startswith(p) for p in self._REASONING_PREFIXES)

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {"model": self.model, "messages": messages, "timeout": timeout}
        # FIX H3: reasoning models use max_completion_tokens and don't support temperature
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = self.max_tokens
        else:
            kwargs["max_tokens"] = self.max_tokens
            kwargs["temperature"] = self.temperature

        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def chat(self, messages: list, timeout: int = 120) -> str:
        kwargs: dict = {"model": self.model, "messages": messages, "timeout": timeout}
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = self.max_tokens
        else:
            kwargs["max_tokens"] = self.max_tokens
            kwargs["temperature"] = self.temperature

        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# Anthropic Claude
# --------------------------------------------------------------------------- #

class AnthropicProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required: pip install 'anthropic>=0.97'")

        kwargs: dict = {"api_key": cfg.api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url

        self.client = anthropic.Anthropic(**kwargs)
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,  # FIX H4: pass timeout to API call
        }
        if system:
            kwargs["system"] = system

        resp = self.client.messages.create(**kwargs)
        return resp.content[0].text if resp.content else ""


# --------------------------------------------------------------------------- #
# Ollama (local)
# --------------------------------------------------------------------------- #

class OllamaProvider(LLMProvider):
    """
    Local Ollama inference — uses the official ollama Python SDK when available,
    falling back to raw HTTP for environments without it.

    Recommended models: llama3.3, gemma3, qwen3, deepseek-r2, mistral
    Install SDK: pip install ollama>=0.6
    """

    def __init__(self, cfg: LLMConfig):
        self.base_url = cfg.base_url or "http://localhost:11434"
        self.model = cfg.model or "llama3.3"
        # Try to import official SDK; fall back to urllib
        try:
            import ollama as _sdk  # noqa: F401
            self._use_sdk = True
        except ImportError:
            self._use_sdk = False

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        if self._use_sdk:
            return self._complete_sdk(prompt, system, timeout)
        return self._complete_http(prompt, system, timeout)

    def _complete_sdk(self, prompt: str, system: str, timeout: int) -> str:
        """FIX M1: Use official ollama SDK (ollama>=0.6) when available."""
        import ollama
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        client = ollama.Client(host=self.base_url)
        resp = client.chat(model=self.model, messages=messages)
        return resp.message.content or ""

    def _complete_http(self, prompt: str, system: str, timeout: int) -> str:
        """Fallback: raw HTTP to /api/generate (compatible with all Ollama versions)."""
        import json
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "prompt": f"{system}\n\n{prompt}" if system else prompt,
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return data.get("response", "")
        except Exception as e:
            raise RuntimeError(
                f"Ollama request failed ({self.base_url}): {e}. "
                f"Is Ollama running? Try: ollama serve"
            ) from e


# --------------------------------------------------------------------------- #
# Echo (testing / dry-run)
# --------------------------------------------------------------------------- #

class EchoProvider(LLMProvider):
    """
    Returns a predictable echo of the prompt. Useful for testing harnesses without API calls.
    Returns the full prompt (not truncated) so integration tests can assert on content.
    """

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        prefix = f"[echo/system: {system[:50]}] " if system else "[echo] "
        return f"{prefix}{prompt}"


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_llm_provider(cfg: LLMConfig) -> LLMProvider:
    """
    Instantiate the correct LLM provider from config.

    Provider selection:
      - "openai"    → OpenAIProvider (standard OpenAI API)
      - "anthropic" → AnthropicProvider
      - "ollama"    → OllamaProvider
      - "custom"    → OpenAIProvider with cfg.base_url set (OpenAI-compatible)
      - "echo"      → EchoProvider (testing)
    """
    p = cfg.provider.lower()

    if p in ("openai", "custom"):
        return OpenAIProvider(cfg)
    elif p == "anthropic":
        return AnthropicProvider(cfg)
    elif p == "ollama":
        return OllamaProvider(cfg)
    elif p == "echo":
        return EchoProvider()
    else:
        raise ValueError(
            f"Unknown LLM provider: '{cfg.provider}'. "
            f"Valid options: openai, anthropic, ollama, custom, echo"
        )
