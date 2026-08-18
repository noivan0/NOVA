#!/usr/bin/env python3
"""nova_llm.py — NOVA LLM 공용 헬퍼 (모든 nova_ 스크립트에서 import)

엔드포인트는 환경변수 CLAUDE_MESSAGES_URL로 설정한다 — 사설 게이트웨이
기본값은 없다. 미설정 시 call_llm()/get_llm_client() 호출 시점에 명확한
에러를 내고 빈 문자열/None을 반환한다.
anthropic SDK base_url 방식은 SDK 버전에 따라 /v1 중복 추가 문제 있음 → urllib 직접 사용.
"""
import json
import os
import re
import ssl
import urllib.request
import urllib.error
import yaml
from pathlib import Path


# 엔드포인트는 환경변수로만 설정 (사설 게이트웨이 기본값 없음)
CLAUDE_MESSAGES_URL = os.environ.get("CLAUDE_MESSAGES_URL", "")


def _get_api_key() -> str:
    """HERMES_API_KEY를 env → .env 파일 순으로 로드"""
    # 1. 환경변수 직접 확인
    key = os.environ.get("HERMES_API_KEY", "")
    if key:
        return key
    # 2. .env 파일에서 로드 (subprocess 환경에서 env 미상속 시)
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("HERMES_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    os.environ["HERMES_API_KEY"] = key  # 이후 호출을 위해 캐시
                    return key
    # 3. config.yaml fallback
    try:
        cfg = yaml.safe_load(open(Path.home() / ".hermes" / "config.yaml"))
        key = cfg["model"]["api_key"]
        if key.startswith("${") and key.endswith("}"):
            key = os.environ.get(key[2:-1], "")
        return key
    except Exception:
        return ""


def _ssl_ctx():
    # P1 fix (2026-08-18, Codex-audited): this used to unconditionally
    # disable both hostname checking and certificate verification for
    # every user — appropriate only for the original author's internal
    # network with a self-signed gateway cert. Default to a real
    # verifying context; opt out explicitly via NOVA_DISABLE_SSL_VERIFY=1
    # for a self-signed internal endpoint.
    if os.environ.get("NOVA_DISABLE_SSL_VERIFY", "").strip().lower() in ("1", "true", "yes", "on"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def call_llm(prompt: str, max_tokens: int = 500,
             model: str = "claude-sonnet-5") -> str:
    """단일 메시지 LLM 호출. 오류 시 빈 문자열 반환.
    마크다운 코드블록(```json ... ```) 자동 제거.
    HTTP 5xx 오류(502 포함) 시 최대 2회 retry (간격 3초)."""
    import time
    key = _get_api_key()
    if not CLAUDE_MESSAGES_URL:
        import sys
        print("[nova_llm] 환경변수 CLAUDE_MESSAGES_URL 미설정 — .env 또는 nova.yaml에서 설정 필요", file=sys.stderr)
        return ""
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                CLAUDE_MESSAGES_URL,
                data=payload,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                }
            )
            with urllib.request.urlopen(req, timeout=90, context=_ssl_ctx()) as r:
                result = json.loads(r.read())
                text = result["content"][0]["text"].strip()
                # 마크다운 코드블록 제거
                text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
                text = re.sub(r'\n?```$', '', text)
                return text.strip()
        except urllib.error.HTTPError as e:
            import sys
            if e.code in (502, 503, 504) and attempt < max_attempts:
                print(f"[nova_llm] HTTP {e.code} — retry {attempt}/{max_attempts - 1} (3초 후)", file=sys.stderr)
                time.sleep(3)
                continue
            print(f"[nova_llm] call_llm HTTP error {e.code}: {e}", file=sys.stderr)
            return ""
        except Exception as e:
            import sys
            print(f"[nova_llm] call_llm error: {e}", file=sys.stderr)
            return ""
    return ""


def get_llm_client(model: str = "claude-sonnet-5"):
    """레거시 호환: anthropic.Anthropic 클라이언트 반환 (가능한 경우).
    URL 충돌 문제가 있으므로 call_llm() 사용 권장.
    base_url은 CLAUDE_ANTHROPIC_BASE_URL 환경변수로 설정해야 하며(없으면
    CLAUDE_MESSAGES_URL에서 '/v1/messages' 접미사를 제거해 재사용),
    둘 다 없으면 명확한 에러를 raise한다."""
    try:
        import anthropic
        import httpx
        key = _get_api_key()
        base_url = os.environ.get("CLAUDE_ANTHROPIC_BASE_URL", "")
        if not base_url and CLAUDE_MESSAGES_URL:
            base_url = CLAUDE_MESSAGES_URL.removesuffix("/v1/messages")
        if not base_url:
            raise RuntimeError(
                "환경변수 CLAUDE_ANTHROPIC_BASE_URL 또는 CLAUDE_MESSAGES_URL 미설정 "
                "— .env 또는 nova.yaml에서 설정 필요"
            )
        return anthropic.Anthropic(
            api_key=key,
            base_url=base_url,
            http_client=httpx.Client(
                # P1 fix (2026-08-18): unconditional verify=False disabled
                # TLS certificate verification for every user, appropriate
                # only for the original author's internal network with a
                # self-signed gateway cert. Default to real verification;
                # opt out explicitly via NOVA_DISABLE_SSL_VERIFY=1 for a
                # self-signed internal endpoint.
                verify=os.environ.get("NOVA_DISABLE_SSL_VERIFY", "").strip().lower()
                not in ("1", "true", "yes", "on"),
                timeout=httpx.Timeout(90.0, connect=10.0)
            )
        )
    except ImportError:
        return None


if __name__ == "__main__":
    # 동작 테스트
    result = call_llm('JSON으로만 답: {"status": "ok"}', max_tokens=20)
    print("LLM test:", result)
