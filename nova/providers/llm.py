"""
nova/providers/llm.py
---------------------
Pluggable LLM provider abstraction.

Supported providers:
  openai     — OpenAI API (GPT-4o, etc.) or any OpenAI-compatible endpoint
  anthropic  — Anthropic Claude API
  ollama     — Local Ollama inference
  custom     — Any OpenAI-compatible endpoint (set NOVA_LLM_BASE_URL)

API Key Round-Robin:
  429 (RateLimitError) 발생 시 ~/.hermes/api_proxy/keys.json 에서
  다음 활성 키로 자동 rotate 후 재시도.
  keys.json이 없으면 단일 키(cfg.api_key)만 사용.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

from nova.core.config import LLMConfig

logger = logging.getLogger(__name__)

# ── Key Pool (keys.json round-robin) ─────────────────────────────────────────

_KEYS_FILE = Path.home() / ".hermes" / "api_proxy" / "keys.json"
_pool_lock = threading.Lock()


def _load_active_keys() -> list[str]:
    """keys.json에서 active + 미만료 키 목록 반환. 없으면 빈 리스트."""
    try:
        if not _KEYS_FILE.exists():
            return []
        data = json.loads(_KEYS_FILE.read_text())
        today = str(date.today())
        return [
            k["key"] for k in data.get("keys", [])
            if k.get("active", True) and k.get("expires", "9999-99-99") >= today
        ]
    except Exception as e:
        logger.debug("[nova/llm] keys.json 로드 실패: %s", e)
        return []


class _KeyRotator:
    """
    429 발생 시 round-robin으로 다음 키 반환.
    keys.json에 키가 없으면 cfg.api_key 단일 키만 사용.
    """
    def __init__(self, base_key: str):
        self._base_key = base_key
        self._idx = 0
        self._keys: list[str] | None = None  # 첫 호출 시 lazy 로드

    def _ensure_loaded(self) -> None:
        if self._keys is None:
            pool = _load_active_keys()
            # base_key를 pool에 포함시키되, 순서는 base_key → 나머지
            if pool:
                others = [k for k in pool if k != self._base_key]
                self._keys = [self._base_key] + others
            else:
                self._keys = [self._base_key]
            logger.debug("[nova/llm] 키풀 초기화: %d개", len(self._keys))

    def current(self) -> str:
        with _pool_lock:
            self._ensure_loaded()
            return self._keys[self._idx % len(self._keys)]

    def rotate(self) -> str:
        """다음 키로 이동 후 반환. 전부 소진되면 첫 키로 wrap-around."""
        with _pool_lock:
            self._ensure_loaded()
            self._idx = (self._idx + 1) % len(self._keys)
            nk = self._keys[self._idx]
            logger.info("[nova/llm] 키 rotate → ...%s", nk[-8:])
            return nk

    def total(self) -> int:
        with _pool_lock:
            self._ensure_loaded()
            return len(self._keys)


# ── Base ─────────────────────────────────────────────────────────────────────

class LLMProvider(ABC):
    """Base class for all LLM providers."""

    @abstractmethod
    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        """Send a prompt and return the text completion."""
        ...

    def chat(self, messages: list, timeout: int = 120) -> str:
        """Send a list of chat messages (OpenAI format) and return the reply."""
        combined = "\n\n".join(
            f"[{m['role'].upper()}]\n{m['content']}" for m in messages
        )
        return self.complete(combined, timeout=timeout)


# ── OpenAI / OpenAI-compatible ───────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """
    Works with:
      - OpenAI (api.openai.com) — openai>=2.0
      - Custom/enterprise OpenAI-compatible endpoints (set base_url)
      - Azure OpenAI (set base_url to Azure endpoint)

    Reasoning models (o1, o3, o4-mini, o1-mini, o1-preview):
      temperature is not supported; use max_completion_tokens instead of max_tokens.
    """

    _REASONING_PREFIXES = ("o1", "o3", "o4")
    _MAX_KEY_RETRIES = 3

    def __init__(self, cfg: LLMConfig):
        try:
            from openai import OpenAI  # noqa: F401
        except ImportError:
            raise ImportError("openai package required: pip install 'openai>=2.0'")

        self._cfg = cfg
        self._base_url = cfg.base_url
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens
        self.temperature = cfg.temperature
        self._rotator = _KeyRotator(cfg.api_key or "sk-placeholder")
        self._build_client(self._rotator.current())

    def _build_client(self, api_key: str) -> None:
        from openai import OpenAI
        if self._base_url:
            self.client = OpenAI(api_key=api_key, base_url=self._base_url)
        else:
            self.client = OpenAI(api_key=api_key)

    def _is_reasoning_model(self) -> bool:
        return any(self.model.startswith(p) for p in self._REASONING_PREFIXES)

    def _make_kwargs(self, messages: list, timeout: int) -> dict:
        kwargs: dict = {"model": self.model, "messages": messages, "timeout": timeout}
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = self.max_tokens
        else:
            kwargs["max_tokens"] = self.max_tokens
            kwargs["temperature"] = self.temperature
        return kwargs

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._call_with_rotation(messages, timeout)

    def chat(self, messages: list, timeout: int = 120) -> str:
        return self._call_with_rotation(messages, timeout)

    def _call_with_rotation(self, messages: list, timeout: int) -> str:
        from openai import RateLimitError
        kwargs = self._make_kwargs(messages, timeout)
        last_exc = None
        for attempt in range(self._MAX_KEY_RETRIES):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except RateLimitError as e:
                last_exc = e
                if self._rotator.total() > 1:
                    new_key = self._rotator.rotate()
                    logger.warning(
                        "[nova/llm] OpenAI 429 감지 (attempt %d/%d), 키 rotate → ...%s",
                        attempt + 1, self._MAX_KEY_RETRIES, new_key[-8:]
                    )
                    self._build_client(new_key)
                    time.sleep(1)
                else:
                    logger.warning("[nova/llm] 429 — 대체 키 없음, 그대로 재시도")
                    time.sleep(2 ** attempt)
        raise last_exc


# ── Anthropic Claude ─────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):

    _MAX_KEY_RETRIES = 3

    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise ImportError("anthropic package required: pip install 'anthropic>=0.97'")

        self._cfg = cfg
        self._base_url = cfg.base_url
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens
        self._rotator = _KeyRotator(cfg.api_key or "placeholder")
        self._build_client(self._rotator.current())

    def _build_client(self, api_key: str) -> None:
        import anthropic, os
        client_kwargs: dict = {"api_key": api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        if os.environ.get("NOVA_SSL_VERIFY", "true").lower() in ("false", "0", "no"):
            try:
                import httpx
                client_kwargs["http_client"] = httpx.Client(verify=False)
            except ImportError:
                pass
        self.client = anthropic.Anthropic(**client_kwargs)

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        try:
            from anthropic import RateLimitError
        except ImportError:
            RateLimitError = Exception  # fallback

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,
        }
        if system:
            kwargs["system"] = system

        last_exc = None
        for attempt in range(self._MAX_KEY_RETRIES):
            try:
                resp = self.client.messages.create(**kwargs)
                return resp.content[0].text if resp.content else ""
            except RateLimitError as e:
                last_exc = e
                if self._rotator.total() > 1:
                    new_key = self._rotator.rotate()
                    logger.warning(
                        "[nova/llm] Anthropic 429 감지 (attempt %d/%d), 키 rotate → ...%s",
                        attempt + 1, self._MAX_KEY_RETRIES, new_key[-8:]
                    )
                    self._build_client(new_key)
                    time.sleep(1)
                else:
                    logger.warning("[nova/llm] 429 — 대체 키 없음, 그대로 재시도")
                    time.sleep(2 ** attempt)
        raise last_exc


# ── Ollama (local) ───────────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    """
    Local Ollama inference — uses the official ollama Python SDK when available,
    falling back to raw HTTP for environments without it.
    """

    def __init__(self, cfg: LLMConfig):
        self.base_url = cfg.base_url or "http://localhost:11434"
        self.model = cfg.model or "llama3.3"
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
        import ollama
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        client = ollama.Client(host=self.base_url)
        resp = client.chat(model=self.model, messages=messages)
        return resp.message.content or ""

    def _complete_http(self, prompt: str, system: str, timeout: int) -> str:
        import json, urllib.request
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


# ── Echo (testing / dry-run) ─────────────────────────────────────────────────

class EchoProvider(LLMProvider):
    """Returns a predictable echo of the prompt. Useful for testing."""

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        prefix = f"[echo/system: {system[:50]}] " if system else "[echo] "
        return f"{prefix}{prompt}"


# ── Factory ───────────────────────────────────────────────────────────────────


class HMGProvider(LLMProvider):
    """Direct httpx provider for HMG Anthropic-compatible endpoint."""

    _MAX_KEY_RETRIES = 3

    def __init__(self, cfg) -> None:
        import httpx
        # CRITICAL-4 FIX: _KeyRotator 통합 — 429/401 시 keys.json round-robin 순환
        self._rotator = _KeyRotator(cfg.api_key or "")
        self._key = self._rotator.current()
        raw = cfg.base_url or "https://internal-llm-gateway.example.com/claude-code/v2"
        self._base = raw.rstrip("/").removesuffix("/v1")
        self.model = cfg.model or "claude-sonnet-4-6"
        self.max_tokens = getattr(cfg, "max_tokens", 4096)
        self.temperature = getattr(cfg, "temperature", 0.7)
        self._client = httpx.Client(timeout=getattr(cfg, "timeout", 120) or 120)

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, timeout)

    def chat(self, messages: list, timeout: int = 120) -> str:
        import httpx
        # Anthropic API: system role -> separate field
        system_text = ""
        chat_msgs   = []
        for m in messages:
            if m.get("role") == "system":
                system_text += m.get("content", "")
            else:
                chat_msgs.append(m)
        if not chat_msgs:
            chat_msgs = [{"role": "user", "content": system_text}]
            system_text = ""
        payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": chat_msgs}
        if system_text:
            payload["system"] = system_text

        last_exc: Exception | None = None
        for attempt in range(self._MAX_KEY_RETRIES):
            self._key = self._rotator.current()
            headers = {
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            try:
                r = self._client.post(
                    f"{self._base}/v1/messages", headers=headers,
                    json=payload, timeout=timeout,
                )
                if r.status_code in (429, 401):
                    # 429: Rate limit  /  401: 만료 키 → 다음 키로 rotate
                    if self._rotator.total() > 1:
                        new_key = self._rotator.rotate()
                        logger.warning(
                            "[nova/hmg] HTTP %d (attempt %d/%d), 키 rotate → ...%s",
                            r.status_code, attempt + 1, self._MAX_KEY_RETRIES, new_key[-8:],
                        )
                        last_exc = RuntimeError(f"HMG HTTP {r.status_code}")
                        continue
                    else:
                        logger.warning("[nova/hmg] HTTP %d — 대체 키 없음", r.status_code)
                r.raise_for_status()
                data = r.json()
                return data["content"][0]["text"]
            except RuntimeError:
                raise
            except Exception as e:
                last_exc = e
                break
        raise RuntimeError(f"HMG API error (모든 키 소진): {last_exc}") from last_exc


def get_llm_provider(cfg: LLMConfig) -> LLMProvider:
    p = cfg.provider.lower()
    if p in ("hmg", "hmg_openai"):
        return HMGProvider(cfg)
    if p in ("openai", "custom", "codex"):
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
            f"Valid options: openai, anthropic, ollama, custom, codex, echo"
        )


# ── HMG 사내 헬퍼 ─────────────────────────────────────────────────────────────

def hmg_embed(text: str, *, api_key: str,
              base_url: str = "https://internal-api-gateway.example.com/hchat-in/api/v3/openai/deployments",
              model: str = "text-embedding-3-large") -> "Optional[list[float]]":
    try:
        import requests
        from typing import Optional  # noqa: F401
        url = f"{base_url.rstrip('/')}/{model}/embeddings"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": text[:8000], "model": model},
            timeout=15,
            verify=False,
        )
        if r.status_code == 200:
            return r.json()["data"][0]["embedding"]
        return None
    except Exception:
        return None


def hmg_image_generate(prompt: str, *, api_key: str,
                        base_url: str = "https://internal-api-gateway.example.com/hchat-in/api/v3/models",
                        model: str = "gemini-3.1-flash-image-preview",
                        fallback_model: str = "gemini-3-pro-image-preview") -> "Optional[str]":
    try:
        import requests
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }

        def _call(m: str) -> "Optional[requests.Response]":
            url = f"{base_url.rstrip('/')}/{m}:generateContent"
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
                verify=False,
            )
            return r if r.status_code == 200 else None

        # 메인 모델 시도
        resp = _call(model)
        # 실패 시 fallback 모델 시도
        if resp is None and fallback_model and fallback_model != model:
            resp = _call(fallback_model)

        if resp is not None:
            data = resp.json()
            for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return part["inlineData"]["data"]
        return None
    except Exception:
        return None
