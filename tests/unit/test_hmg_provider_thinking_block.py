"""
tests/unit/test_hmg_provider_thinking_block.py — HMGProvider extended
thinking 응답 파싱 회귀 테스트 (2026-08-28)

배경: gpt-5.6-terra 대비 메인 모델(claude-sonnet-5, HMGProvider) 실효성
A/B 벤치마크를 실제 사내 API로 3회 반복 실행하던 중, 특정 프롬프트에서
HMGProvider.chat()이 100% 재현 가능하게 `KeyError: 'text'`로 실패하는
것을 발견했다.

근본 원인: claude-sonnet-5가 extended thinking을 사용하면 응답의
content[0]이 {"type": "thinking", ...}이고, 실제 텍스트는 content[1]
(또는 그 이후)의 {"type": "text", "text": "..."}에 담긴다. 기존 코드는
`data["content"][0]["text"]`로 첫 번째 블록을 무조건 텍스트로 가정해
KeyError가 발생했다.

수정: content 배열을 순회해 type == "text"인 블록을 찾아 반환한다.
텍스트 블록이 전혀 없으면 원인 파악이 쉬운 명시적 RuntimeError를 낸다.

이 테스트는 실제 사내 API를 호출하지 않고 httpx 응답을 monkeypatch로
mock하여, thinking 블록이 섞인 실제 관측 응답 형태를 재현한다.
"""
from __future__ import annotations

import pytest

from nova.core.config import LLMConfig
from nova.providers.llm import HMGProvider


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json_data = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


@pytest.fixture
def hmg_provider():
    cfg = LLMConfig(
        provider="hmg",
        model="claude-sonnet-5",
        api_key="fake-key-for-test",
        base_url="https://fake.internal/api",
        max_tokens=1024,
    )
    return HMGProvider(cfg)


def test_thinking_block_before_text_block_is_parsed_correctly(hmg_provider, monkeypatch):
    """실제 관측된 응답 형태 재현: content[0]=thinking, content[1]=text.
    이전 버그: content[0]["text"]로 KeyError. 수정 후: type 필드로 탐색."""
    fake_response_json = {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "abc123"},
            {"type": "text", "text": '{"verdict": "FAIL", "reasons": ["test"]}'},
        ],
        "stop_reason": "end_turn",
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, fake_response_json)

    monkeypatch.setattr(hmg_provider._client, "post", fake_post)
    result = hmg_provider.chat([{"role": "user", "content": "test"}])
    assert result == '{"verdict": "FAIL", "reasons": ["test"]}'


def test_text_only_block_still_works(hmg_provider, monkeypatch):
    """thinking을 쓰지 않는 일반 응답(content[0]=text)도 여전히 정상 동작해야 한다."""
    fake_response_json = {
        "content": [{"type": "text", "text": "hello world"}],
        "stop_reason": "end_turn",
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, fake_response_json)

    monkeypatch.setattr(hmg_provider._client, "post", fake_post)
    result = hmg_provider.chat([{"role": "user", "content": "test"}])
    assert result == "hello world"


def test_multiple_thinking_blocks_before_text(hmg_provider, monkeypatch):
    """thinking 블록이 여러 개 있어도 마지막에 나오는 text 블록을 찾아야 한다."""
    fake_response_json = {
        "content": [
            {"type": "thinking", "thinking": "step 1"},
            {"type": "thinking", "thinking": "step 2"},
            {"type": "text", "text": "final answer"},
        ],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, fake_response_json)

    monkeypatch.setattr(hmg_provider._client, "post", fake_post)
    result = hmg_provider.chat([{"role": "user", "content": "test"}])
    assert result == "final answer"


def test_no_text_block_raises_clear_error(hmg_provider, monkeypatch):
    """text 블록이 전혀 없으면(예: tool_use만 있는 경우) 명확한 에러를
    내야 한다 — 조용히 빈 문자열을 반환하거나 KeyError로 죽으면 안 된다."""
    fake_response_json = {
        "content": [{"type": "tool_use", "id": "x", "name": "foo", "input": {}}],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, fake_response_json)

    monkeypatch.setattr(hmg_provider._client, "post", fake_post)
    with pytest.raises(RuntimeError, match="no 'text' content block"):
        hmg_provider.chat([{"role": "user", "content": "test"}])


def test_empty_content_array_raises_clear_error(hmg_provider, monkeypatch):
    fake_response_json = {"content": []}

    def fake_post(*args, **kwargs):
        return _FakeResponse(200, fake_response_json)

    monkeypatch.setattr(hmg_provider._client, "post", fake_post)
    with pytest.raises(RuntimeError):
        hmg_provider.chat([{"role": "user", "content": "test"}])
