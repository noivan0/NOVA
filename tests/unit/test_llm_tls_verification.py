"""tests/unit/test_llm_tls_verification.py — regression tests for
CodexResponsesProvider's TLS verification default.

SECURITY-017 (2026-08-18, deep audit round 6): the P1 audit (2026-08-18)
swept AnthropicProvider/hmg_embed/hmg_image_generate in nova/providers/llm.py
to make TLS verification opt-in (defaulting to on), but missed
CodexResponsesProvider, which called `requests.post(..., verify=False)`
unconditionally with no way to opt back into verification -- a real MITM
exposure for every request this provider makes. Fixed to default to
verification on, disabled only via NOVA_SSL_VERIFY=false.
"""
import pytest

from nova.core.config import LLMConfig
from nova.providers.llm import CodexResponsesProvider


@pytest.fixture(autouse=True)
def _clean_ssl_env(monkeypatch):
    monkeypatch.delenv("NOVA_SSL_VERIFY", raising=False)
    yield


def _make_cfg(**overrides):
    defaults = dict(
        provider="codex",
        model="gpt-5",
        api_key="test-key",
        base_url="https://example.invalid/openai/responses",
        max_tokens=1024,
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def test_tls_verification_enabled_by_default():
    provider = CodexResponsesProvider(_make_cfg())
    assert provider._verify_tls is True


def test_tls_verification_disabled_via_env_false(monkeypatch):
    monkeypatch.setenv("NOVA_SSL_VERIFY", "false")
    provider = CodexResponsesProvider(_make_cfg())
    assert provider._verify_tls is False


def test_tls_verification_disabled_via_env_0(monkeypatch):
    monkeypatch.setenv("NOVA_SSL_VERIFY", "0")
    provider = CodexResponsesProvider(_make_cfg())
    assert provider._verify_tls is False


def test_tls_verification_stays_enabled_for_other_values(monkeypatch):
    monkeypatch.setenv("NOVA_SSL_VERIFY", "true")
    provider = CodexResponsesProvider(_make_cfg())
    assert provider._verify_tls is True


def test_complete_passes_verify_flag_to_requests_post(monkeypatch):
    """Regression test for the exact bug: verify=False must never be
    hardcoded into the requests.post() call regardless of _verify_tls."""
    provider = CodexResponsesProvider(_make_cfg())
    captured = {}

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}

    def _fake_post(url, headers=None, json=None, timeout=None, verify=None):
        captured["verify"] = verify
        return _FakeResponse()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    result = provider.complete("hello")
    assert result == "ok"
    assert captured["verify"] is True  # must match provider._verify_tls (default True)
