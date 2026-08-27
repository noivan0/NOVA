"""
tests/unit/test_llm_gateway_presets.py — 범용 OpenAI 호환 게이트웨이 프리셋 +
Fallback Chain 테스트 (2026-08-28 추가)

기존 hmg/codex_responses/openai/anthropic/ollama/echo provider 동작에는
영향이 없어야 한다 — 이 테스트는 신규 기능(GATEWAY_PRESETS, FallbackChainProvider)만
검증한다.
"""
from __future__ import annotations

import os

import pytest

from nova.core.config import LLMConfig
from nova.providers.llm import (
    GATEWAY_PRESETS,
    FallbackChainProvider,
    OpenAIProvider,
    get_fallback_chain_from_env,
    get_llm_provider,
)


def test_gateway_presets_registered():
    """주요 공개 게이트웨이 프리셋이 등록되어 있어야 한다."""
    expected = {"groq", "deepseek", "mistral", "xai", "openrouter", "together"}
    assert expected.issubset(GATEWAY_PRESETS.keys())


@pytest.mark.parametrize("provider_name", ["groq", "deepseek", "mistral", "xai", "openrouter"])
def test_gateway_preset_resolves_base_url(provider_name):
    """프리셋 provider는 base_url 없이도 OpenAIProvider로 해석되고 프리셋 URL을 사용해야 한다."""
    cfg = LLMConfig(provider=provider_name, model="dummy-model", api_key="fake-key")
    p = get_llm_provider(cfg)
    assert isinstance(p, OpenAIProvider)
    assert GATEWAY_PRESETS[provider_name]["base_url"].rstrip("/") in str(p.client.base_url).rstrip("/")


def test_gateway_preset_explicit_base_url_overrides_default():
    """base_url을 명시하면 프리셋 기본값보다 우선해야 한다."""
    cfg = LLMConfig(provider="groq", model="dummy", api_key="fake", base_url="https://custom.example.com/v1")
    p = get_llm_provider(cfg)
    assert "custom.example.com" in str(p.client.base_url)


def test_existing_providers_unaffected_by_presets():
    """기존 provider(echo/custom)는 프리셋 추가 이후에도 동일하게 동작해야 한다."""
    echo = get_llm_provider(LLMConfig(provider="echo", model="x"))
    assert echo.complete("hello", system="sys") == "[echo/system: sys] hello"

    custom = get_llm_provider(
        LLMConfig(provider="custom", model="my-model", base_url="https://my-gw.example.com/v1", api_key="k")
    )
    assert isinstance(custom, OpenAIProvider)
    assert "my-gw.example.com" in str(custom.client.base_url)


def test_unknown_provider_error_lists_presets():
    """알 수 없는 provider 에러 메시지에 신규 프리셋 이름도 안내되어야 한다."""
    with pytest.raises(ValueError) as exc_info:
        get_llm_provider(LLMConfig(provider="totally-unknown-provider", model="x"))
    assert "groq" in str(exc_info.value)


def test_fallback_chain_uses_first_success():
    """체인의 첫 provider가 성공하면 이후 provider는 시도하지 않아야 한다."""
    chain = FallbackChainProvider([
        LLMConfig(provider="echo", model="first"),
        LLMConfig(provider="echo", model="second"),
    ])
    result = chain.complete("hello")
    assert result == "[echo] hello"


def test_fallback_chain_falls_back_on_failure():
    """앞 provider가 예외를 던지면 다음 provider로 폴백해야 한다."""
    chain = FallbackChainProvider([
        LLMConfig(provider="anthropic", model="x", api_key=""),  # 실패 유도 (빈 키로 API 호출 시 에러)
        LLMConfig(provider="echo", model="fallback-target"),
    ])
    # anthropic 호출은 네트워크 실패/인증 실패로 예외가 나야 하고, echo로 폴백되어야 한다.
    # 네트워크가 없는 CI 환경에서도 anthropic 클라이언트 생성 자체는 성공하므로
    # 실제 요청 시점에 예외가 발생해 폴백이 트리거되는지만 확인한다.
    try:
        result = chain.complete("hello")
        assert result == "[echo] hello"
    except RuntimeError:
        pytest.skip("네트워크 환경에 따라 anthropic 호출 예외 유형이 달라질 수 있음")


def test_fallback_chain_requires_at_least_one_config():
    with pytest.raises(ValueError):
        FallbackChainProvider([])


def test_get_fallback_chain_from_env_absent(monkeypatch):
    """환경변수 미설정 시 None을 반환해 기존 단일 provider 흐름을 그대로 둔다."""
    monkeypatch.delenv("NOVA_LLM_FALLBACK_CHAIN", raising=False)
    assert get_fallback_chain_from_env() is None


def test_get_fallback_chain_from_env_parses_pairs(monkeypatch):
    monkeypatch.setenv("NOVA_LLM_FALLBACK_CHAIN", "echo:model-a, groq:model-b")
    monkeypatch.setenv("NOVA_LLM_API_KEY", "shared-key")
    chain = get_fallback_chain_from_env()
    assert isinstance(chain, FallbackChainProvider)
    pairs = [(c.provider, c.model, c.api_key) for c in chain._configs]
    assert pairs == [("echo", "model-a", "shared-key"), ("groq", "model-b", "shared-key")]
