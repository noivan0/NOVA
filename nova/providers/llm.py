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
from typing import Optional

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
      - OpenAI (api.openai.com)
      - Custom/enterprise OpenAI-compatible endpoints (set base_url)
      - Azure OpenAI (set base_url to Azure endpoint)
    """

    def __init__(self, cfg: LLMConfig):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required: pip install openai")

        kwargs = {"api_key": cfg.api_key or "sk-placeholder"}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url

        self.client = OpenAI(**kwargs)
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout=timeout,
        )
        return resp.choices[0].message.content or ""

    def chat(self, messages: list, timeout: int = 120) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout=timeout,
        )
        return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# Anthropic Claude
# --------------------------------------------------------------------------- #

class AnthropicProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package required: pip install anthropic")

        self.client = anthropic.Anthropic(api_key=cfg.api_key)
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        resp = self.client.messages.create(**kwargs)
        return resp.content[0].text if resp.content else ""


# --------------------------------------------------------------------------- #
# Ollama (local)
# --------------------------------------------------------------------------- #

class OllamaProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        self.base_url = cfg.base_url or "http://localhost:11434"
        self.model = cfg.model

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get("response", "")


# --------------------------------------------------------------------------- #
# Echo (testing / dry-run)
# --------------------------------------------------------------------------- #

class EchoProvider(LLMProvider):
    """Returns the prompt back. Useful for testing harnesses without API calls."""

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        return f"[echo] {prompt[:200]}"


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
