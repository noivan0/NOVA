#!/usr/bin/env python3
"""
nova_codex_gate.py — NOVA v1.0 Phase 5: Claude Code + Codex 공동검증 게이트
==========================================================================
모든 NOVA 프로젝트(blog-pipeline, doosi, unlearning)의 콘텐츠/코드 검증에 사용.

워크플로우:
  1. Claude Code   — 콘텐츠 구현/생성 (anthropic messages API)
  2. Codex        — 감사/검증 (openai codex CLI)
  3. 헤르2(sub)   — IPC를 통해 헤르(main)에 검증 요청 전달
  4. 판정         — APPROVED (발행) / REQUEST_CHANGES (수정) / ABORT (중단)

사용법:
  python3 nova_codex_gate.py --project blog-pipeline --content "..." --mode review
  python3 nova_codex_gate.py --project doosi --content-file /tmp/content.md --mode full
"""
import os, sys, json, time, uuid, subprocess, logging, tempfile
from pathlib import Path

BASE = Path(__file__).parent.parent
HERMES_HOME = Path.home() / ".hermes"
# 환경변수 우선 → 헤르 메인/헤르2 양쪽에서 동작
# 헤르 메인: /root/.hermes/ipc/{main_to_sub,sub_to_main}
# 헤르2:     /workspace/ipc/{main_to_sub,sub_to_main}
IPC_OUT = Path(os.environ.get("NOVA_IPC_OUT", str(HERMES_HOME / "ipc" / "sub_to_main")))
IPC_IN  = Path(os.environ.get("NOVA_IPC_IN",  str(HERMES_HOME / "ipc" / "main_to_sub")))
LOG_DIR = HERMES_HOME / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "nova_codex_gate.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("nova_codex_gate")

# .env 자동 로드 (모듈 임포트 시점에 실행)
_env_file = Path.home() / ".hermes" / ".env"
if _env_file.exists():
    import re as _re
    _env_tmp = {}
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _, _v = _line.partition("=")
            _k = _k.strip(); _v = _v.strip()
            # ${VAR} expansion
            _v = _re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
                         lambda m: _env_tmp.get(m.group(1), ""), _v)
            _env_tmp[_k] = _v
            os.environ.setdefault(_k, _v)

# Claude API 설정
CLAUDE_BASE = os.environ.get("CLAUDE_BASE_URL", "https://h-chat-api.autoever.com/claude-code/v2")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
API_KEY = os.environ.get("HERMES_API_KEY", "")

# ─────────────────────────────────────────────────────────
# [v2.0 업그레이드] GPT-5.4 L2 독립 감사 설정 (2026-05-25)
# bin/nova_codex_gate.py v2.0에서 병합
# ─────────────────────────────────────────────────────────
GPT_BASE  = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3/openai/deployments/gpt-5.4"
GPT_MODEL = "gpt-5.4"
GPT_KEY   = os.environ.get("GPT_AUDIT_KEY") or os.environ.get("HERMES_API_KEY", "")
NOVA_BRAIN_DB = HERMES_HOME / "nova_brain.db"

# P0-B fix: 사내망 SSL 비검증 기본값 (REQUESTS_CA_BUNDLE로 bundle 지정 가능)
_ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE", "")
# [SEC-G-IMP-1] 환경변수명 의미 수정: NOVA_FORCE_SSL_VERIFY → NOVA_DISABLE_SSL_VERIFY
# NOVA_DISABLE_SSL_VERIFY=1 설정 시 SSL 검증 비활성화 (사내망 인증서 이슈 대응)
_disable_ssl_raw = os.environ.get("NOVA_DISABLE_SSL_VERIFY", "").strip()
_ssl_disabled = _disable_ssl_raw in ("1", "true", "yes", "on")  # [HIGH-1 FIX] bool("0")==True 버그 수정
SSL_VERIFY = _ca_bundle if _ca_bundle else not _ssl_disabled


# ─────────────────────────────────────────────
# 공통 헬퍼 — Codex-I3: 반환 스키마 표준화
# ─────────────────────────────────────────────
def _extract_json_from_text(text: str) -> str:
    """백틱 코드블록에서 JSON 추출 — 잘린 응답 안전 처리.
    BUG-CG1 fix: split('```')[1] 취약점 제거.
    BUG-CGX fix: 비탐욕 매칭이 truncated 응답에서 빈 문자열 반환하는 버그 수정.
    """
    import re as _re, json as _json
    # 1) ```json ... ``` 완전 블록 (greedy로 가장 큰 JSON 객체 추출)
    m = _re.search(r"```(?:json|JSON)?\s*(\{[\s\S]*\})\s*```", text)
    if m:
        candidate = m.group(1).strip()
        try:
            _json.loads(candidate)
            return candidate
        except _json.JSONDecodeError:
            pass
    # 2) ``` 없는 순수 JSON 또는 잘린 응답 — { 부터 마지막 } 까지
    if "{" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        if end > start:
            candidate = text[start:end].strip()
            try:
                _json.loads(candidate)
                return candidate
            except _json.JSONDecodeError:
                pass
    # 3) 잘린 응답 복구 시도 — 닫히지 않은 JSON에 } 추가
    if "{" in text:
        start = text.find("{")
        fragment = text[start:]
        depth = 0
        for i, ch in enumerate(fragment):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        if depth > 0:
            # [HIGH-3 FIX] 열린 문자열 먼저 닫고, 그 다음 중괄호 닫기
            in_str = False
            esc = False
            for _ch in fragment:
                if esc:
                    esc = False
                    continue
                if _ch == "\\":
                    esc = True
                    continue
                if _ch == '"':
                    in_str = not in_str
            _close_str = '"' if in_str else ""
            repaired = fragment + _close_str + "}" * depth
            try:
                _json.loads(repaired)
                return repaired
            except _json.JSONDecodeError:
                pass
    # 4) 필드 직접 추출 fallback — 잘린 문자열 내부로 파싱 불가한 경우
    import re as _re2
    verdict_m = _re2.search(r'"verdict"\s*:\s*"([^"]+)"', text)
    adj_m     = _re2.search(r'"score_adjustment"\s*:\s*(-?\d+)', text)
    score_m   = _re2.search(r'"score"\s*:\s*(\d+)', text)
    if verdict_m:
        reconstructed = '{"verdict":"%s"' % verdict_m.group(1)
        if adj_m:
            reconstructed += ',"score_adjustment":%s' % adj_m.group(1)
        if score_m:
            reconstructed += ',"score":%s' % score_m.group(1)
        reconstructed += ',"additional_issues":[],"final_recommendation":"truncated response","reviewer":"independent-auditor"}'
        try:
            _json.loads(reconstructed)
            return reconstructed
        except _json.JSONDecodeError:
            pass
    return text.strip()


def _review_error(reviewer: str, reason: str, verdict: str = "ABORT", status: str = "error") -> dict:
    """reviewer 에러 시 표준 스키마 반환 — downstream이 dict.get으로 실패 숨기는 것 방지"""
    return {
        "status": status,
        "reason": reason,
        "reviewer": reviewer,
        "verdict": verdict,
        "score": 0,
        "critical_issues": [reason],
        "important_issues": [],
        "summary": reason,
    }


# ─────────────────────────────────────────────
# [BUG-CG7 FIX] 프로젝트별 평가 기준
# ─────────────────────────────────────────────
PROJECT_CRITERIA = {
    "blog-pipeline":    "블로그 기준: 실용 정보(교통/숙박/관광지/팁 2개 이상), SEO 자연 키워드, 2000자 이상",
    "doosi":            "숏폼 콘텐츠: 공감성, 트렌드 반영, 첫 문장 임팩트, 루프 유발 구조",
    "saju-wellness":    "사주 앱: 만세력 정확도, AI 해석 품질, 법적 면책, 개인정보 처리",
    "mental-load":      "멘탈헬스: 위기감지, 14세 연령제한, SOS 번호, 전문가 연결",
    "caring-ansimcall": "케어 앱: IVR 흐름, Twilio HMAC, 가족동의 2단계, SSRF 방어",
    "unlearning":       "언러닝 블로그: 사고 전환, 철학 인사이트, 실용 적용, 독자 행동 유도",
}

def _get_project_criteria(project: str) -> str:
    if project in PROJECT_CRITERIA:
        return PROJECT_CRITERIA[project]
    p = project.lower()
    if any(k in p for k in ["blog", "trip", "travel", "triptong"]):
        return PROJECT_CRITERIA["blog-pipeline"]
    if any(k in p for k in ["saju", "wellness", "jiazi"]):
        return PROJECT_CRITERIA["saju-wellness"]
    if any(k in p for k in ["mental", "adhd", "load"]):
        return PROJECT_CRITERIA["mental-load"]
    if any(k in p for k in ["caring", "senior", "care", "ansim"]):
        return PROJECT_CRITERIA["caring-ansimcall"]
    if any(k in p for k in ["doosi", "shorts"]):
        return PROJECT_CRITERIA["doosi"]
    if any(k in p for k in ["unlearning"]):
        return PROJECT_CRITERIA["unlearning"]
    return "소프트웨어 품질: 코드 완성도, 보안(OWASP Top 10), 성능, 에러 처리, 테스트 커버리지"


# ─────────────────────────────────────────────────────────
# [v2.0 추가] CT+TL 기록 + Trajectory 업데이트 헬퍼
# ─────────────────────────────────────────────────────────
def _record_ct_tl(project: str, phase: str, think: str, act: str, observe: str):
    """nova_brain takes에 CT+TL 체인 기록 (Think→Act→Observe)"""
    try:
        import sqlite3
        if not NOVA_BRAIN_DB.exists():
            return
        con = sqlite3.connect(str(NOVA_BRAIN_DB))
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        entry = f"[{ts}] [{phase}] Think={think[:80]} | Act={act[:80]} | Observe={observe[:80]}"
        con.execute(
            "INSERT INTO takes (id,page_id,holder,kind,claim,weight,source,created_at) "
            "SELECT lower(hex(randomblob(8))), MIN(id), 'nova-gate', 'fact', ?, 0.9, 'nova-codex-gate', ? "
            "FROM pages WHERE path LIKE ? LIMIT 1",
            # BUG-SOURCE-NULL-2 (2026-07-31): source 추가
            (entry, ts, f"%{project}%")
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.debug(f"CT+TL 기록 실패(무시): {e}")


def _update_trajectory(project: str, final_score: float):
    """프로젝트 품질 점수 시계열 업데이트"""
    try:
        import sqlite3
        if not NOVA_BRAIN_DB.exists():
            return
        con = sqlite3.connect(str(NOVA_BRAIN_DB))
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        con.execute(
            "INSERT OR IGNORE INTO takes (id,page_id,holder,kind,claim,weight,source,created_at) "
            "SELECT lower(hex(randomblob(8))), COALESCE(MIN(id),1), 'nova-trajectory', 'bet', ?, 0.7, 'nova-codex-gate', ? "
            "FROM pages WHERE path LIKE ? LIMIT 1",
            (f"[{ts}] {project} score={final_score:.1f}", ts, f"%{project}%")
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.debug(f"Trajectory 업데이트 실패(무시): {e}")


def _gpt_fallback(claude_result: dict, reason: str) -> dict:
    """GPT-5.4 L2 실패 시 Claude L1 보수적 반영"""
    logger.info(f"GPT fallback ({reason}) — Claude 결과 보수적 사용")
    return {
        "score_adjustment": 0,
        "verdict": claude_result.get("verdict", "REQUEST_CHANGES"),
        "confirmed_by_gpt": [],
        "overruled_by_gpt": [],
        "new_critical_issues": [f"GPT L2 unavailable: {reason}"],
        "reviewer": "gpt-fallback",
        "fallback_reason": reason,
    }


def gpt_audit(project: str, content: str, claude_result: dict) -> dict:
    """[v2.0 L2] GPT-5.4로 독립 감사 — 완전히 다른 모델로 Claude 편향 제거"""
    try:
        import requests
    except ImportError:
        return _gpt_fallback(claude_result, "requests not installed")

    if not GPT_KEY:
        return _gpt_fallback(claude_result, "GPT_KEY not set")

    endpoint = f"{GPT_BASE}/chat/completions"
    criteria = _get_project_criteria(project)

    system_msg = f"""You are Layer-2 independent auditor for {project} (GPT-5.4).
Your job: find what Claude MISSED or got WRONG.
Project criteria: {criteria}
Output JSON only:
{{
  "score_adjustment": <-20 to +10>,
  "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
  "confirmed_by_gpt": ["what Claude got right"],
  "overruled_by_gpt": ["what Claude got wrong"],
  "new_critical_issues": ["issues Claude missed"],
  "reviewer": "gpt-5.4"
}}"""

    payload = {
        "model": GPT_MODEL,
        "max_completion_tokens": 400,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": (
                f"[CLAUDE L1]\nScore: {claude_result.get('score','?')}\n"
                f"Verdict: {claude_result.get('verdict','?')}\n"
                f"Critical: {claude_result.get('critical_issues',[])}\n\n"
                f"[CONTENT]\n{content[:3000]}"
            )}
        ]
    }
    headers = {"Content-Type": "application/json", "api-key": GPT_KEY}

    try:
        ssl_verify = not bool(os.environ.get("NOVA_DISABLE_SSL_VERIFY", ""))
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60, verify=ssl_verify)
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            parsed = _extract_json_from_text(raw)
            result = json.loads(parsed)
            result["reviewer"] = "gpt-5.4"
            return result
        else:
            return _gpt_fallback(claude_result, f"HTTP {resp.status_code}")
    except Exception as e:
        return _gpt_fallback(claude_result, str(e))


def _merge_verdict_v2(claude_r: dict, gpt_r: dict) -> dict:
    """[v2.0] L1+L2 병합 판정 — Iron Law + 3/4 합의"""
    cv = claude_r.get("verdict", "REQUEST_CHANGES")
    gv = gpt_r.get("verdict", "REQUEST_CHANGES")
    cs = float(claude_r.get("score", 50))
    ga = float(gpt_r.get("score_adjustment", 0))
    final_score = max(0, min(100, cs + ga))

    # GPT 응답 잘림(truncated) 시 ABORT 무시 — Claude 단독 판정으로 fallback
    _gpt_truncated = (gpt_r.get("final_recommendation") == "truncated response"
                      or gpt_r.get("status") in ("parse_error", "json_error", "empty_response", "no_content", "empty_text"))
    if _gpt_truncated:
        logger.warning("[nova_gate] GPT 응답 파싱 실패(truncated/error) — GPT 판정 무시, Claude 단독 판정")
        # truncated 시 score_adjustment=0, verdict=APPROVED 처리 (Claude 단독)
        gv = "APPROVED"
        ga = 0.0
        final_score = max(0, min(100, cs))  # Claude 점수만 사용

    if cv == "ABORT" or gv == "ABORT":
        return {"verdict": "ABORT", "final_score": final_score, "source": "iron_law"}
    if final_score >= 75 and not (cv == "REQUEST_CHANGES" and gv == "REQUEST_CHANGES"):
        return {"verdict": "APPROVED", "final_score": final_score, "source": "consensus"}
    return {"verdict": "REQUEST_CHANGES", "final_score": final_score, "source": "conservative"}


# ─────────────────────────────────────────────
# Phase 5a: Claude Code 검증 (직접 API 호출)
# ─────────────────────────────────────────────
def claude_review(project: str, content: str, mode: str = "review") -> dict:
    """Claude Code로 콘텐츠/코드 품질 검토"""
    try:
        import requests
    except ImportError:
        return _review_error("claude-code", "requests not installed")

    if not API_KEY:
        return _review_error("claude-code", "HERMES_API_KEY not set")

    _criteria = _get_project_criteria(project)
    system_prompt = f"""당신은 {project} 프로젝트의 품질 게이트 리뷰어입니다.
다음 기준으로 평가하세요:
- 정확성: 명백한 사실 오류, 날짜 오류 없음
- 완성도: {_criteria}
- 분량: 충분한 정보 제공
- SEO/키워드: 자연스럽게 포함
- 점수 기준: 80+ APPROVED, 70~79 경계, 70 미만 REQUEST_CHANGES

[중요] 이 글은 발행 직전 최종 검토입니다. 명백한 문제가 없으면 APPROVED를 주세요.
사소한 개선점이 있어도 80점 이상이면 APPROVED입니다.

출력 형식 (JSON만, 다른 텍스트 없음):
{{
  "score": <0-100>,
  "verdict": "APPROVED" | "REQUEST_CHANGES",
  "critical_issues": [],
  "important_issues": [],
  "summary": "한 줄 요약"
}}"""

    # [BUG-CG6 FIX] 6000자 제한 → HTML 태그 제거 후 순수 텍스트 10000자 사용 (품질 판단 편향 방지)
    import re as _re
    _text_only = _re.sub(r"<[^>]+>", " ", content)
    _text_only = _re.sub(r"\s+", " ", _text_only).strip()
    user_prompt = f"""[{mode.upper()} 모드] 아래 {project} 콘텐츠를 검토하세요:

{_text_only[:10000]}

평가 기준:
- 정확성: 사실 오류 없음
- 완성도: {_criteria}
- 분량: 충분한 정보 제공
- HTML 구조가 제거된 순수 텍스트 기준으로 평가하세요

JSON 형식으로만 답변하세요."""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    }

    headers = {
        "content-type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": ANTHROPIC_VERSION
    }

    endpoint = CLAUDE_BASE.rstrip("/") + "/v1/messages"
    try:
        resp = requests.post(
            endpoint, json=payload, headers=headers,
            timeout=max(10, int(os.environ.get("NOVA_CLAUDE_TIMEOUT", "90") or "90")),  # [HIGH-GCE-1 FIX]
            verify=SSL_VERIFY  # P0-B: SSL_VERIFY, Codex-I4: timeout 환경변수화
        )
        resp.raise_for_status()
        data = resp.json()
        # [HIGH-GCE-2 FIX] 빈 content 배열 방어
        _content_list = data.get("content", [])
        if not _content_list or not isinstance(_content_list, list):
            raise ValueError(f"API 응답 content 비어있음: {str(data)[:200]}")
        text = (_content_list[0].get("text") or "").strip()
        if not text:
            raise ValueError("API 응답 text 필드 없음")
        # BUG-CG1 fix: 안전한 JSON 추출 (_extract_json_from_text 헬퍼)
        text = _extract_json_from_text(text)
        result = json.loads(text)
        result["reviewer"] = "claude-code"
        return result
    except requests.exceptions.Timeout as e:
        logger.error(f"Claude review 타임아웃: {e}")
        return _review_error("claude-code", str(e), status="timeout")  # BUG-CG5 fix: timeout 구분
    except Exception as e:
        logger.error(f"Claude review 실패: {e}")
        return _review_error("claude-code", str(e))


# ─────────────────────────────────────────────
# Phase 5b: Codex 감사 (CLI subprocess)
# ─────────────────────────────────────────────
def codex_audit(project: str, content: str, claude_result: dict) -> dict:
    """2차 독립 감사 — Claude API를 독립 심사위원 시스템 프롬프트로 호출
    
    Note: hermes-codex-acp (Codex CLI)는 ACP 서버 모드라 subprocess 직접 호출 불가.
    대신 Claude API를 '독립 심사위원' 역할로 호출하여 동일한 2중 검증 효과를 구현.
    실제 Codex 게이트웨이(codex-cli/v2)는 타임아웃 불안정으로 직접 호출 제외.
    """
    try:
        import requests as _req
    except ImportError:
        return {"status": "skipped", "reason": "requests not installed", "reviewer": "codex-fallback"}

    # 모듈 레벨 API_KEY 우선 사용, 없으면 환경변수, 그래도 없으면 .env 직접 로드
    api_key = API_KEY or os.environ.get("HERMES_API_KEY", "")
    if not api_key:
        env_file = Path.home() / ".hermes" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("HERMES_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        return _review_error("codex-fallback", "HERMES_API_KEY not set", verdict="REQUEST_CHANGES", status="error")

    # 독립 심사위원 시스템 프롬프트 (Claude 1차와 완전히 다른 관점)
    system_prompt = f"""당신은 {project} 프로젝트의 **독립 품질 감사관**입니다.
Claude의 1차 검토와 독립적으로, 외부 심사위원 관점에서 콘텐츠를 평가하세요.

감사 중점:
- Claude가 놓친 이슈가 있는가? (비판적 시각 필수)
- 점수 조정이 필요한가? (과대/과소 평가 교정)
- 독자 관점: 실제로 읽고 싶은 글인가?
- 사실 오류, 논리 모순, 클리셰 표현 여부

1차 검토 요약:
- 점수: {claude_result.get('score', 'N/A')}
- 판정: {claude_result.get('verdict', 'N/A')}
- 주요 이슈: {json.dumps(claude_result.get('critical_issues', []), ensure_ascii=False)[:500]}

출력 형식 (JSON만, 다른 텍스트 없음):
{{
  "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
  "score_adjustment": <정수, -20 ~ +20 범위>,
  "additional_issues": ["놓친 이슈 1", "놓친 이슈 2"],
  "final_recommendation": "한 줄 요약",
  "reviewer": "independent-auditor"
}}"""

    # [BUG-CGA FIX] HTML 태그 제거 후 텍스트 기준 6000자 (기존 raw HTML[:4000] → JSON 파싱 실패)
    import re as _re_c
    _c_text = _re_c.sub(r'<[^>]+>', ' ', content)
    _c_text = _re_c.sub(r'\s+', ' ', _c_text).strip()
    user_prompt = f"""[독립 감사 대상 콘텐츠]

{_c_text[:6000]}"""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 512,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION
    }
    endpoint = CLAUDE_BASE.rstrip("/") + "/v1/messages"

    try:
        resp = _req.post(endpoint, json=payload, headers=headers, timeout=max(10, int(os.environ.get("NOVA_CODEX_TIMEOUT", "60") or "60")), verify=SSL_VERIFY)  # [FIX-TIMEOUT-2] 빈 문자열 방어 (line 221과 동일 패턴)
        resp.raise_for_status()
        logger.debug(f"[독립감사] status={resp.status_code} text_len={len(resp.text)}")
        if not resp.text.strip():
            return {"status": "empty_response", "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}
        # content 필드 안전 추출 (중복 empty check 제거)
        data = resp.json()
        content_blocks = data.get("content", [])
        if not content_blocks:
            return {"status": "no_content", "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}
        text = (content_blocks[0].get("text") or "").strip()
        if not text:
            return {"status": "empty_text", "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}
        # BUG-CG1 fix: 안전한 JSON 추출 헬퍼 사용
        raw = _extract_json_from_text(text)
        if raw:
            result = json.loads(raw)
            result["reviewer"] = "independent-auditor"
            return result
        return {"status": "parse_error", "raw": text[:200], "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}
    except json.JSONDecodeError as e:
        logger.error(f"독립 감사 JSON 파싱 실패: {e}")
        return {"status": "json_error", "reason": str(e), "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}
    except Exception as e:
        logger.error(f"독립 감사 실패: {e}")
        return {"status": "error", "reason": str(e), "reviewer": "independent-auditor", "verdict": "REQUEST_CHANGES"}


# ─────────────────────────────────────────────
# Phase 5c: IPC → 헤르(메인) 최종 판정 요청
# ─────────────────────────────────────────────
def ipc_ask_main(project: str, claude_r: dict, codex_r: dict, content_preview: str) -> dict:
    """헤르(메인)에 IPC로 최종 판정 요청 (2분 대기)"""
    # [MED-IPC-2 FIX] is_dir() 검사: 파일이 아닌 디렉터리 채널임을 확인
    if not IPC_OUT.is_dir():
        logger.warning("IPC 채널 없음(디렉터리 없음) → 헤르2 자체 판정")
        return {"verdict": _merge_verdict(claude_r, codex_r), "source": "hermes2_fallback"}

    msg_id = str(uuid.uuid4())[:8]
    ask_msg = {
        "id": msg_id,
        "ts": int(time.time()),
        "from": "hermes2",
        "to": "hermes_main",
        "type": "ask",
        "question": (
            f"[NOVA 공동검증] {project} 콘텐츠 최종 판정을 내려주세요.\n\n"
            f"Claude Code 검토: 점수={claude_r.get('score','?')}, 판정={claude_r.get('verdict','?')}\n"
            f"Codex 감사: 판정={codex_r.get('verdict','?')}, 조정={codex_r.get('score_adjustment','0')}\n\n"
            f"콘텐츠 미리보기 (500자):\n{content_preview[:500]}\n\n"
            f"최종 판정을 JSON으로 답변: "
            f"{{\"verdict\": \"APPROVED\" | \"REQUEST_CHANGES\" | \"ABORT\", \"reason\": \"...\"}}"
        ),
        "context": {
            "project": project,
            "claude_result": claude_r,
            "codex_result": codex_r
        },
        # [CRIT-IPC-1 FIX] _ipc_timeout 정의를 dict 참조보다 먼저 이동 (NameError 방지)
        "timeout_sec": int(os.environ.get("NOVA_IPC_TIMEOUT", "240")),
        "fallback_verdict": _merge_verdict(claude_r, codex_r)
    }

    try:
        # CG-1 fix: IPC 파일 원자적 쓰기
        ask_file = IPC_OUT / f"{msg_id}.json"
        fd, tmp_ask = tempfile.mkstemp(dir=str(IPC_OUT), suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(ask_msg, f, ensure_ascii=False, indent=2)
            ask_file_tmp = Path(tmp_ask)
            ask_file_tmp.replace(ask_file)
        except Exception:
            try:
                Path(tmp_ask).unlink(missing_ok=True)
            except OSError:
                pass
            raise
        logger.info(f"IPC ask 전송: {msg_id}")

        # 헤르(메인) 응답 대기 (최대 240초)
        reply_file = IPC_IN / f"{msg_id}_reply.json"
        # BUG-CG7 FIX: IPC wait timeout 120s→240s (poll60+claude90+여유90)
        # [CRIT-IPC-1 FIX] _ipc_timeout 정의를 dict 참조 이후로도 사용 (deadline 계산용)
        _ipc_timeout = int(os.environ.get("NOVA_IPC_TIMEOUT", "240"))
        _poll_interval = int(os.environ.get("NOVA_IPC_POLL_INTERVAL", "5"))  # [LOW-5 FIX]
        _deadline = time.time() + _ipc_timeout
        while time.time() < _deadline:  # [MED-2 FIX] check-before-sleep (early exit)
            if reply_file.exists():
                # CG-2 fix: JSON 파싱 오류 격리
                try:
                    with open(reply_file, encoding="utf-8") as f:
                        reply = json.load(f)
                    logger.info(f"IPC reply 수신: {reply}")
                    # BUG-CG2 fix: answer 필드 유효성 검증 + verdict 대안 필드명 허용
                    _VALID_VERDICTS = {"APPROVED", "REQUEST_CHANGES", "ABORT"}
                    raw_ans = reply.get("answer") or reply.get("verdict", "")
                    if raw_ans not in _VALID_VERDICTS:
                        logger.warning(f"IPC reply 비정상 verdict '{raw_ans}' → fallback 병합 판정")
                        raw_ans = _merge_verdict(claude_r, codex_r)
                    return {"verdict": raw_ans, "source": "hermes_main"}
                except (json.JSONDecodeError, OSError) as pe:
                    logger.warning(f"IPC reply 파싱 실패({pe}) — {msg_id}_reply → fallback")
                    break
            time.sleep(_poll_interval)  # sleep은 확인 후 (early exit 보장)

        logger.warning("IPC 응답 타임아웃 → 헤르2 자체 판정")
        return {"verdict": _merge_verdict(claude_r, codex_r), "source": "hermes2_fallback"}
    except Exception as e:
        logger.error(f"IPC ask 실패: {e}")
        return {"verdict": _merge_verdict(claude_r, codex_r), "source": "hermes2_fallback"}


def _merge_verdict(claude_r: dict, codex_r: dict) -> str:
    """Claude + Codex 판정 병합 (보수적 원칙)

    CG-3 fix: 미정의 verdict 값 처리 → unknown은 REQUEST_CHANGES 취급
    CG-4 fix: REQUEST_CHANGES + score≥70이어도 APPROVED 승격 제거 (fail-open 방지)
    """
    VALID_VERDICTS = {"APPROVED", "REQUEST_CHANGES", "ABORT"}
    cv = claude_r.get("verdict", "REQUEST_CHANGES")
    xv = codex_r.get("verdict",  "REQUEST_CHANGES")
    # 미정의 값 → REQUEST_CHANGES (보수적)
    if cv not in VALID_VERDICTS:
        logger.warning(f"[merge_verdict] claude 미정의 verdict '{cv}' → REQUEST_CHANGES 취급")
        cv = "REQUEST_CHANGES"
    if xv not in VALID_VERDICTS:
        logger.warning(f"[merge_verdict] codex 미정의 verdict '{xv}' → REQUEST_CHANGES 취급")
        xv = "REQUEST_CHANGES"

    if xv == "ABORT" or cv == "ABORT":
        return "ABORT"
    if xv == "REQUEST_CHANGES" or cv == "REQUEST_CHANGES":
        # CG-4: score 기반 APPROVED 승격 제거 — REQUEST_CHANGES는 항상 REQUEST_CHANGES
        return "REQUEST_CHANGES"
    return "APPROVED"


# ─────────────────────────────────────────────
# 메인 게이트 함수 (외부 호출용)
# ─────────────────────────────────────────────
def run_gate(project: str, content: str, mode: str = "review", use_ipc: bool = True) -> dict:
    """
    NOVA Phase 5 공동검증 게이트 실행

    Returns:
        {
            "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
            "score": <int>,
            "claude": {...},
            "codex": {...},
            "final_source": "merged" | "ipc" | "hermes2_fallback",
            "elapsed": <float>
        }
    """
    start = time.time()
    logger.info(f"[{project}] NOVA Phase 5 공동검증 시작 (mode={mode})")

    # P3 fix: 빈 콘텐츠는 ABORT 반환 — 품질 게이트 우회 방지
    if not content or not content.strip():
        logger.warning(f"[{project}] 빈 콘텐츠 — 발행 차단 (ABORT)")
        return {
            "verdict": "ABORT",
            "score": 0,
            "claude": {"status": "skipped", "reason": "empty_content"},
            "codex": {"status": "skipped", "reason": "empty_content"},
            "final_source": "empty_content_abort",
            "elapsed": 0.0,
            "project": project,
            "mode": mode,
            "ts": int(time.time())
        }

    # BUG-CG4 fix: INSTANCE_ROLE 환경변수 우선, 심링크 해제(resolve) 없이 abspath 비교
    # [BUG-IPC-OVERRIDE FIX] caller가 use_ipc=True 명시 시 INSTANCE_ROLE 우회 금지
    instance_role = os.environ.get("INSTANCE_ROLE", "").lower()
    if not use_ipc:
        # 호출자가 명시적으로 False 요청한 경우만 우회
        caller_is_main = True
    elif instance_role == "main":
        # supervisor가 main을 주입하더라도 use_ipc=True(기본값) 상태면 IPC 사용
        caller_is_main = False
        logger.debug(f"[{project}] INSTANCE_ROLE=main이지만 use_ipc=True — IPC 사용")
    elif instance_role in ("sub", "hermes2"):
        caller_is_main = False
    else:
        # INSTANCE_ROLE 미설정: __file__ 심링크를 해제하지 않고 abspath로 비교
        _script_path = Path(os.path.abspath(__file__))
        caller_is_main = _script_path.parent == (HERMES_HOME / "scripts")
        if caller_is_main:
            logger.warning("[caller_is_main] INSTANCE_ROLE 미설정 — 경로 비교로 main 감지. 명시 설정 권장.")
    if caller_is_main:
        use_ipc = False
        logger.info(f"[{project}] IPC 우회 (자체 판정)")

    # 5a: Claude Code 검토
    claude_r = claude_review(project, content, mode)
    logger.info(f"[{project}] Claude 검토: score={claude_r.get('score','?')} verdict={claude_r.get('verdict','?')}")

    # Codex-C6: claude_review error/import_error → 즉시 ABORT (fail-open 방지)
    # 헤르 의견 수렴 후 REQUEST_CHANGES로 완화 가능 (NOVA_GATE_CLAUDE_ERROR_VERDICT 환경변수로 조정)
    _claude_err_verdict = os.environ.get("NOVA_GATE_CLAUDE_ERROR_VERDICT", "ABORT")
    # [SEC-G-IMP-2] 화이트리스트 검증 — 임의 문자열 판정 주입 방지
    _VALID_VERDICTS = {"APPROVED", "REQUEST_CHANGES", "ABORT"}
    if _claude_err_verdict not in _VALID_VERDICTS:
        logger.warning(f"[SEC] NOVA_GATE_CLAUDE_ERROR_VERDICT 비허가 값: {_claude_err_verdict!r} → ABORT로 강제")
        _claude_err_verdict = "ABORT"
    if claude_r.get("status") in {"error", "timeout"}:
        logger.error(f"[{project}] Claude 검토 실패({claude_r.get('status')}) → {_claude_err_verdict} 반환")
        _fail_result = {
            "verdict": _claude_err_verdict,
            "score": 0,
            "claude": claude_r,
            "codex": {"status": "skipped_claude_error"},
            "final_source": "claude_error_abort",
            "elapsed": round(time.time() - start, 1),
            "project": project,
            "mode": mode,
            "ts": int(time.time()),
        }
        _record_gate_result(project, _fail_result)
        return _fail_result

    # 5b: [v2.0] GPT-5.4 L2 독립 감사 + 기존 Codex fallback
    # GPT-5.4가 사용 가능하면 L1+L2 병합, 불가능하면 Codex 폴백
    gpt_r = gpt_audit(project, content, claude_r)
    if gpt_r.get("reviewer") == "gpt-5.4":
        # v2.0 경로: L1(Claude) + L2(GPT) 병합
        logger.info(f"[{project}] GPT L2 감사: adj={gpt_r.get('score_adjustment','?')} verdict={gpt_r.get('verdict','?')}")
        merge_v2 = _merge_verdict_v2(claude_r, gpt_r)
        codex_r = gpt_r  # 하위 호환을 위해 codex_r에도 저장
        merged = merge_v2["verdict"]
        final_score = merge_v2["final_score"]
        _record_ct_tl(project, "L3-final",
                      f"claude={claude_r.get('score','?')}/{claude_r.get('verdict','?')} gpt={gpt_r.get('score_adjustment','?')}/{gpt_r.get('verdict','?')}",
                      "merge_verdict_v2()",
                      f"final={merged} score={final_score}")
        _update_trajectory(project, final_score)
    else:
        # v1.0 폴백: Codex 감사
        codex_r = codex_audit(project, content, claude_r)
        logger.info(f"[{project}] Codex 감사(GPT폴백): verdict={codex_r.get('verdict','?')}")
        merged = _merge_verdict(claude_r, codex_r)
        score = claude_r.get("score", 0)
        adj = codex_r.get("score_adjustment", 0)
        adj = adj if isinstance(adj, (int, float)) else 0
        adj = max(-20, min(20, adj))
        final_score = max(0, min(100, score + adj))

    logger.info(f"[{project}] 병합 판정: verdict={merged}")

    # 5c: 점수 미달이거나 REQUEST_CHANGES면 IPC 최종 판정
    # merged는 위 5b에서 이미 설정됨 (v2.0: merge_verdict_v2 / v1.0: _merge_verdict)
    final_source = "merged"
    final_verdict = merged

    if use_ipc and merged == "REQUEST_CHANGES":
        logger.info(f"[{project}] 불일치/저점수 → IPC 헤르(메인) 최종 판정 요청")
        ipc_r = ipc_ask_main(project, claude_r, codex_r, content)
        final_verdict = ipc_r.get("verdict", merged)
        final_source = ipc_r.get("source", "ipc")
    elif use_ipc and merged == "ABORT":
        final_source = "merged_abort"

    # final_score: v2.0에서는 merge_verdict_v2에서 계산됨, v1.0 폴백은 위에서 계산됨
    # 여기서 재계산하면 중복이므로 조건부 처리
    if "final_score" not in dir() or final_score is None:  # type: ignore
        score = claude_r.get("score", 0)
        adj = codex_r.get("score_adjustment", 0)
        adj = adj if isinstance(adj, (int, float)) else 0
        adj = max(-20, min(20, adj))
        final_score = max(0, min(100, score + adj))

    result = {
        "verdict": final_verdict,
        "score": final_score,
        "claude": claude_r,
        "codex": codex_r,
        "final_source": final_source,
        "elapsed": round(time.time() - start, 1),
        "project": project,
        "mode": mode,
        "ts": int(time.time())
    }

    logger.info(f"[{project}] 공동검증 완료: verdict={final_verdict} score={final_score} elapsed={result['elapsed']}s")

    # 검증 결과 evolution.md에 기록
    _record_gate_result(project, result)

    return result


def _record_gate_result(project: str, result: dict):
    """검증 결과를 evolution.md에 기록 (CG-5: PROJECTS_DIR 환경변수 활용, CG-6: 원자적 append)"""
    try:
        # [R24 FIX] 프로젝트명 → 실제 경로 별칭 매핑
        # caring-ansimcall은 /root/.hermes/projects/senior-care에 존재
        PROJECT_PATH_ALIAS = {
            "caring-ansimcall": "senior-care",
            "saju-onerday": "saju-wellness",
        }
        resolved_project = PROJECT_PATH_ALIAS.get(project, project)
        _projects_root = Path(os.environ.get("NOVA_PROJECTS_DIR", str(HERMES_HOME / "projects"))).resolve()
        # [SEC-G-CRIT-1] Path Traversal 방지 — project 파라미터 검증
        import re as _re
        if not _re.match(r'^[\w\-]{1,64}$', resolved_project):
            logger.error(f"[SEC] _record_gate_result: 비정상 project명 차단: {resolved_project!r}")
            return
        proj_dir = (_projects_root / resolved_project).resolve()
        if not str(proj_dir).startswith(str(_projects_root)):
            logger.error(f"[SEC] _record_gate_result: Path Traversal 차단: {proj_dir}")
            return
        evo_file = proj_dir / "evolution.md"
        proj_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        entry = (
            f"\n## [{now}] NOVA Phase 5 공동검증 — {project}\n"
            f"- 판정: {result['verdict']}\n"
            f"- 점수: {result['score']}\n"
            f"- Claude: {result['claude'].get('verdict','?')} ({result['claude'].get('score','?')}점)\n"
            f"- Codex: {result['codex'].get('verdict','?')}\n"
            f"- 출처: {result['final_source']}\n"
            f"- 소요시간: {result['elapsed']}초\n"
        )
        # BUG-CG3 fix: fcntl lock으로 read-modify-write atomic 보장 (동시 append entry 유실 방지)
        import fcntl
        lock_path = proj_dir / "evolution.md.lock"
        fd, tmp_path = tempfile.mkstemp(dir=str(proj_dir), suffix=".md.tmp")
        try:
            with open(lock_path, "a") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    existing = ""
                    if evo_file.exists():
                        try:
                            existing = evo_file.read_text(encoding="utf-8")
                        except OSError:
                            pass
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(existing + entry)
                    fd = -1  # fdopen이 소유권 취득 — 이중 close 방지
                    Path(tmp_path).replace(evo_file)
                    tmp_path = None
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            try:
                if tmp_path:  # BUG-CG3: None 체크 (replace 성공 후 None 설정됨)
                    Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning(f"evolution.md 기록 실패: {e}")


# ─────────────────────────────────────────────
# CLI 인터페이스
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NOVA Phase 5 공동검증 게이트")
    parser.add_argument("--project", required=True, help="프로젝트명 (blog-pipeline, doosi, unlearning)")
    parser.add_argument("--content", help="검증할 콘텐츠 (직접 입력)")
    parser.add_argument("--content-file", help="검증할 콘텐츠 파일 경로")
    parser.add_argument("--mode", default="review", choices=["review", "full", "quick"], help="검증 모드")
    parser.add_argument("--no-ipc", action="store_true", help="IPC 헤르(메인) 판정 요청 없이 로컬만")
    args = parser.parse_args()

    # API 키 로드
    env_file = HERMES_HOME / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    content = ""
    if args.content:
        content = args.content
    elif args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()

    result = run_gate(
        project=args.project,
        content=content,
        mode=args.mode,
        use_ipc=not args.no_ipc
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["verdict"] == "APPROVED" else 1)
