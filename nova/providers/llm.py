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
    """Direct httpx provider for a generic Anthropic Messages-API-compatible
    enterprise/custom gateway.

    base_url has no default — it must be supplied via cfg.base_url
    (NOVA_LLM_BASE_URL / provider-specific base_url in nova.yaml). This
    provider name ("hmg") is kept for backward compatibility with existing
    deployments; new custom-gateway users should generally prefer the
    "custom" provider (OpenAI-compatible) or add a new preset in
    GATEWAY_PRESETS if it follows the OpenAI schema instead.
    """

    _MAX_KEY_RETRIES = 3

    def __init__(self, cfg) -> None:
        import httpx
        if not cfg.base_url:
            raise ValueError(
                "HMGProvider requires base_url to be set "
                "(NOVA_LLM_BASE_URL env var or llm.base_url in nova.yaml)"
            )
        # CRITICAL-4 FIX: _KeyRotator 통합 — 429/401 시 keys.json round-robin 순환
        self._rotator = _KeyRotator(cfg.api_key or "")
        self._key = self._rotator.current()
        self._base = cfg.base_url.rstrip("/").removesuffix("/v1")
        self.model = cfg.model or "claude-sonnet-4-5"
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


# ── HMG Codex Responses API (사내 게이트웨이 전용) ────────────────────────────

class CodexResponsesProvider(LLMProvider):
    """
    HMG 사내 API 게이트웨이의 OpenAI Responses API 스키마 전용 provider.
    base_url 자체가 완전한 엔드포인트(.../openai/responses)이며, 표준 OpenAI SDK의
    chat.completions.create()가 만드는 base_url+"/chat/completions" 경로는 게이트웨이에
    존재하지 않아 404가 발생한다. requests로 {"model","input"} POST 직접 호출.
    응답 스키마: {"output": [{"content": [{"type":"output_text","text":"..."}]}]}
    """

    _MAX_KEY_RETRIES = 3

    def __init__(self, cfg: LLMConfig):
        if not cfg.base_url:
            raise ValueError("CodexResponsesProvider requires base_url")
        self._cfg = cfg
        self._url: str = cfg.base_url
        self.model = cfg.model
        self.max_tokens = cfg.max_tokens
        self._rotator = _KeyRotator(cfg.api_key or "")

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        import requests
        full_input = f"{system}\n\n{prompt}" if system else prompt
        payload = {"model": self.model, "input": full_input}
        last_exc = None
        for attempt in range(self._MAX_KEY_RETRIES):
            api_key = self._rotator.current()
            try:
                r = requests.post(
                    self._url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                    verify=False,
                )
                if r.status_code == 429 and self._rotator.total() > 1:
                    self._rotator.rotate()
                    time.sleep(1)
                    continue
                r.raise_for_status()
                data = r.json()
                # output[] 중 type=="message"인 항목의 content[0].text 추출
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                return c.get("text", "")
                return ""
            except Exception as e:
                last_exc = e
                if attempt < self._MAX_KEY_RETRIES - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"CodexResponsesProvider 호출 실패: {last_exc}") from last_exc

    def chat(self, messages: list, timeout: int = 120) -> str:
        combined = "\n\n".join(f"[{m.get('role','user')}] {m.get('content','')}" for m in messages)
        return self.complete(combined, timeout=timeout)


def get_llm_provider(cfg: LLMConfig) -> LLMProvider:
    p = cfg.provider.lower()
    if p in ("hmg", "hmg_openai"):
        return HMGProvider(cfg)
    # codex_responses: HMG 사내 게이트웨이 전용 — base_url 자체가 /responses 엔드포인트이며
    # OpenAI SDK의 chat.completions.create()(base_url + "/chat/completions")를 쓰면
    # 404 "No static resource .../responses/chat/completions" 발생. 표준 Responses API
    # 스키마({"model","input"} POST, 응답 output[0].content[0].text)로 직접 호출해야 함.
    # (2026-08-10 정밀감사에서 evaluate_kpi_codex phase가 매번 404로 조용히 skip되어
    #  cross-family judge 합의가 사실상 무력화된 것을 실증 확인 → 근본 수정)
    if p == "codex" and cfg.base_url and "/responses" in cfg.base_url:
        return CodexResponsesProvider(cfg)
    if p in GATEWAY_PRESETS:
        # 2026-08-28: 범용 OpenAI 호환 게이트웨이 프리셋 — base_url을 몰라도
        # provider 이름만으로 즉시 사용 가능 (기존 hmg/codex_responses/openai/
        # anthropic/ollama/echo 동작에는 영향 없음, 신규 provider 이름만 추가)
        preset = GATEWAY_PRESETS[p]
        resolved_cfg = LLMConfig(
            provider="openai",
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url or preset["base_url"],
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            timeout=cfg.timeout,
        )
        return OpenAIProvider(resolved_cfg)
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
            f"Valid options: openai, anthropic, ollama, custom, codex, echo, "
            f"{', '.join(sorted(GATEWAY_PRESETS))}"
        )


# ── 범용 OpenAI 호환 게이트웨이 프리셋 (2026-08-28 추가) ─────────────────────────
#
# 기존 hmg/codex_responses/openai/anthropic/ollama/echo provider는 전혀
# 변경하지 않는다. 여기 등록된 이름을 NOVA_LLM_PROVIDER에 지정하면 base_url을
# 몰라도 바로 사용 가능해지는 "별칭"일 뿐이며, 내부적으로는 OpenAIProvider를
# 재사용한다(OpenAI Chat Completions 호환 스펙을 따르는 게이트웨이 전제).
# NOVA_LLM_BASE_URL을 명시적으로 지정하면 프리셋 값보다 우선한다.
GATEWAY_PRESETS: dict[str, dict[str, str]] = {
    "groq":       {"base_url": "https://api.groq.com/openai/v1",
                   "note": "Groq — LPU 초고속 추론 (Llama/Mixtral/Gemma 등)"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",
                   "note": "DeepSeek 공식 API"},
    "mistral":    {"base_url": "https://api.mistral.ai/v1",
                   "note": "Mistral AI 공식 API"},
    "xai":        {"base_url": "https://api.x.ai/v1",
                   "note": "xAI Grok 공식 API"},
    "moonshot":   {"base_url": "https://api.moonshot.cn/v1",
                   "note": "Moonshot Kimi 공식 API"},
    "zhipu":      {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                   "note": "Zhipu GLM 공식 API"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "note": "OpenRouter — 수백 개 모델 단일 게이트웨이"},
    "together":   {"base_url": "https://api.together.xyz/v1",
                   "note": "Together AI — 오픈모델 호스팅"},
    "fireworks":  {"base_url": "https://api.fireworks.ai/inference/v1",
                   "note": "Fireworks AI — 오픈모델 호스팅"},
    "perplexity": {"base_url": "https://api.perplexity.ai",
                   "note": "Perplexity — 웹검색 결합 모델"},
    "azure_openai_gateway": {"base_url": "",
                   "note": "Azure OpenAI — base_url 필수 지정 (프리셋 없음, 별칭만 등록)"},
}


# ── Fallback Chain (2026-08-28 추가) ─────────────────────────────────────────
#
# 여러 provider/model을 우선순위 체인으로 등록해두고, 앞 provider가 실패
# (예외 발생)하면 자동으로 다음 provider로 넘어간다. oh-my-hermes의
# mixture-of-models 카테고리 체인 개념을 참고했으나, NOVA는 브랜드/카테고리
# 대신 순수 provider+model 페어 리스트로 단순화했다.
#
# 기존 단일 provider 사용 흐름(get_llm_provider)에는 전혀 영향 없음 — 이 클래스는
# 명시적으로 생성해서 쓸 때만 관여한다.

class FallbackChainProvider(LLMProvider):
    """여러 LLMConfig를 순서대로 시도하는 래퍼. 전부 실패하면 마지막 예외를 올린다."""

    def __init__(self, configs: list[LLMConfig]):
        if not configs:
            raise ValueError("FallbackChainProvider requires at least one LLMConfig")
        self._configs = configs

    def complete(self, prompt: str, system: str = "", timeout: int = 120) -> str:
        last_exc: Exception | None = None
        for i, cfg in enumerate(self._configs):
            try:
                provider = get_llm_provider(cfg)
                return provider.complete(prompt, system=system, timeout=timeout)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[nova/fallback_chain] provider %d/%d (%s/%s) 실패: %s — 다음으로 폴백",
                    i + 1, len(self._configs), cfg.provider, cfg.model, e,
                )
                continue
        raise RuntimeError(f"FallbackChainProvider: 모든 provider 실패 ({len(self._configs)}개 시도)") from last_exc

    def chat(self, messages: list, timeout: int = 120) -> str:
        last_exc: Exception | None = None
        for i, cfg in enumerate(self._configs):
            try:
                provider = get_llm_provider(cfg)
                return provider.chat(messages, timeout=timeout)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[nova/fallback_chain] provider %d/%d (%s/%s) 실패: %s — 다음으로 폴백",
                    i + 1, len(self._configs), cfg.provider, cfg.model, e,
                )
                continue
        raise RuntimeError(f"FallbackChainProvider: 모든 provider 실패 ({len(self._configs)}개 시도)") from last_exc


def get_fallback_chain_from_env() -> "FallbackChainProvider | None":
    """NOVA_LLM_FALLBACK_CHAIN 환경변수에서 폴백 체인을 구성한다.

    형식: "provider1:model1,provider2:model2,..."
    예:    NOVA_LLM_FALLBACK_CHAIN="hmg:claude-sonnet-4-6,groq:llama-3.3-70b-versatile,ollama:llama3.3"

    각 provider별 api_key/base_url은 기존 NOVA_LLM_API_KEY / provider별 개별
    환경변수(NOVA_<PROVIDER>_API_KEY 등은 미지원 — 현재는 단일 마스터 키
    NOVA_LLM_API_KEY를 모든 체인 항목에 공용으로 사용)를 그대로 재사용한다.
    설정 안 되어 있으면 None 반환 — 기존 단일 provider 흐름은 완전히 그대로 유지된다.
    """
    import os
    raw = os.environ.get("NOVA_LLM_FALLBACK_CHAIN", "").strip()
    if not raw:
        return None
    api_key = os.environ.get("NOVA_LLM_API_KEY", "")
    configs: list[LLMConfig] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        provider, model = item.split(":", 1)
        configs.append(LLMConfig(provider=provider.strip(), model=model.strip(), api_key=api_key))
    if not configs:
        return None
    return FallbackChainProvider(configs)


# ── Enterprise/custom gateway helpers ────────────────────────────────────────
#
# These standalone helpers (hmg_embed, hmg_image_generate) target an
# OpenAI-compatible enterprise embeddings/images gateway. base_url has no
# default here — callers must supply their own gateway URL. `verify_ssl`
# defaults to True; set it to False only for gateways behind a
# self-signed/internal CA (matches common enterprise proxy setups).

def hmg_embed(text: str, *, api_key: str, base_url: str,
              model: str = "text-embedding-3-large",
              verify_ssl: bool = True) -> "Optional[list[float]]":
    try:
        import requests
        from typing import Optional  # noqa: F401
        url = f"{base_url.rstrip('/')}/{model}/embeddings"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": text[:8000], "model": model},
            timeout=15,
            verify=verify_ssl,
        )
        if r.status_code == 200:
            return r.json()["data"][0]["embedding"]
        return None
    except Exception:
        return None


def hmg_image_generate(prompt: str, *, api_key: str, base_url: str,
                        model: str = "gpt-image-1",
                        fallback_model: str = "",
                        fallback_base_url: str = "",
                        verify_ssl: bool = True) -> "Optional[str]":
    """OpenAI-compatible enterprise gateway 경유 이미지 생성.

    메인 모델은 OpenAI Images API 형식(POST {base_url}, {model, prompt, size, n}
    → data[0].b64_json)을 쓰고, fallback_model/fallback_base_url이 지정되면
    실패 시 Gemini generateContent 형식(POST {fallback_base_url}/{model}:generateContent)
    으로 폴백한다. 두 API는 요청/응답 스키마가 다르므로 각각 별도 처리한다.
    base_url/fallback_base_url 모두 기본값이 없다 — 호출자가 반드시 지정해야 한다.
    """
    try:
        import requests

        # 1) 메인 모델 (OpenAI Images API 형식)
        try:
            resp = requests.post(
                base_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
                timeout=120,
                verify=verify_ssl,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data") or []
                if items and "b64_json" in items[0]:
                    return items[0]["b64_json"]
        except Exception:
            pass

        # 2) 폴백: Gemini generateContent 형식
        if fallback_model and fallback_base_url:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            }
            url = f"{fallback_base_url.rstrip('/')}/{fallback_model}:generateContent"
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60,
                verify=verify_ssl,
            )
            if r.status_code == 200:
                data = r.json()
                for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        return part["inlineData"]["data"]
        return None
    except Exception:
        return None
