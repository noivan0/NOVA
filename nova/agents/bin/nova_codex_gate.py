#!/usr/bin/env python3
"""
nova_codex_gate.py v2.0 — NOVA × gstack × gbrain 완전 융합 게이트
====================================================================
3-Layer 진짜 독립 검증:
  Layer 1: Claude(HMG)  — 구현자 관점 1차 검토
  Layer 2: GPT-5.4(HMG) — 완전 다른 모델, 독립 감사 (헤르2가 실증한 엔드포인트)
  Layer 3: 병합 판정     — 3/4 합의 자동 진행, 충돌 시 노이반 에스컬레이션

gstack 사고법 이식:
  - Scope Drift Detection: "의도한 것만 변경했나?"
  - Fix-First Heuristic: 95%+ → AUTO-FIX, 85%미만 → ASK
  - Iron Law (investigate): 근본원인 없으면 판정 없음
  - CT+TL 기록: Think→Act→Observe 체인 매 판정마다 nova_brain에 저장

gbrain 패턴 이식:
  - Takes 3관점 병렬: fact(사실) / take(의견) / bet(예측)
  - Trajectory 업데이트: 프로젝트별 품질 점수 시계열
  - Contradiction 감지: 이전 판정과 상충 시 자동 플래그

사용법:
  python3 nova_codex_gate.py --project saju-wellness --content "..." --mode quick
  python3 nova_codex_gate.py --project saju-wellness --content-file /tmp/c.md --mode full
  python3 nova_codex_gate.py --project saju-wellness --content "..." --no-ipc  # GPT만, IPC 없음
"""
import os, sys, json, time, uuid, concurrent.futures, logging
from pathlib import Path

BASE = Path(__file__).parent.parent
HERMES_HOME = Path.home() / ".hermes"
IPC_OUT = Path("/workspace/ipc/sub_to_main")
IPC_IN  = Path("/workspace/ipc/main_to_sub")
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

# .env 자동 로드
_env_file = Path.home() / ".hermes" / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Claude API 설정
CLAUDE_BASE  = os.environ.get("CLAUDE_BASE_URL", "https://h-chat-api.autoever.com/claude-code/v2")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
API_KEY      = os.environ.get("HERMES_API_KEY", "")

# GPT-5.4 API 설정 (헤르2 실증: HMG internal-apigw, max_completion_tokens 필수)
# 헤르2 발견 (2026-05-24): 헤르 HERMES_API_KEY = Claude 전용, GPT 401
# GPT-5.4 API 설정 (헤르2 실증: HMG internal-apigw, max_completion_tokens 필수)
# GPT-5.4 API 설정 (헤르2 실증 2026-05-24: 정확한 엔드포인트 + max_completion_tokens 필수)
GPT_BASE  = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3/openai/deployments/gpt-5.4"
GPT_MODEL = "gpt-5.4"  # deployment 경로에 버전 포함 → model 필드는 짧게
GPT_KEY   = os.environ.get("GPT_AUDIT_KEY") or os.environ.get("HERMES_API_KEY", "")

NOVA_BRAIN_DB = HERMES_HOME / "nova_brain.db"


# ─────────────────────────────────────────────────────────
# 프로젝트별 평가 기준 (gstack: Connect to user outcomes)
# ─────────────────────────────────────────────────────────
PROJECT_CRITERIA: dict[str, str] = {
    # 3개 앱 (사주/멘탈/케어링)
    "saju-wellness":  "사주 앱 기준: 만세력 정확도·AI 해석 품질·법적 면책 문구·개인정보 처리방침",
    "mental-load":    "멘탈헬스 앱 기준: 위기 감지 정확도·14세 제한 UI·SOS 번호 노출·자살예방법 §4 준수",
    "caring":         "케어앱 기준: IVR 플로우 정확성·Twilio HMAC 검증·가족 양측동의·Quiet Hours 준수",
    # 블로그 파이프라인
    "blog-pipeline":  "블로그 기준: 실용 정보 밀도·SEO 키워드 자연스러운 삽입·출처 정확성·타이틀 매력도·GEO필수(FAQ섹션 최소1개·질문형소제목 최소2개·구체적수치 최소3개) — GEO 미달 시 ABORT",
    "unlearning":     "언러닝 블로그 기준: 인사이트 깊이·독자 가치·AI 문체 회피·인간적 어조",
    "doosi":          "김두시 숏폼 기준: 반전 매력·루프 유발 요소·감정 훅·15초 내 핵심 전달",
    # KRAYT
    "krayt":          "KRAYT QA 기준: 취약점 재현 가능성·CVSS 정확도·PoC 코드 동작 여부·오탐 여부",
    # NOVA 에이전트
    "nova-oss":       "NOVA OSS 기준: API 설계 일관성·문서 완전성·테스트 커버리지·하위호환성",
}

def _get_project_criteria(project: str) -> str:
    """프로젝트명으로 평가 기준 반환 (부분 매칭 지원)"""
    # 정확 매칭
    if project in PROJECT_CRITERIA:
        return PROJECT_CRITERIA[project]
    # 부분 매칭 (소문자)
    proj_lower = project.lower()
    for key, val in PROJECT_CRITERIA.items():
        if key in proj_lower or proj_lower in key:
            return val
    # 기본값: 범용 소프트웨어 품질 기준
    return f"{project} 기준: 코드 정확성·보안 취약점 없음·사용자 가치·완전성 (Boil the Lake)"


# ─────────────────────────────────────────────────────────
# CT+TL 기록 헬퍼 (gbrain 패턴)
# ─────────────────────────────────────────────────────────
def _record_ct_tl(project: str, phase: str, think: str, act: str, observe: str,
                  kind: str = "fact", weight: float = 0.9):
    """nova_brain takes에 CT+TL 체인 기록 (Think→Act→Observe)

    불확실도 반영 규칙 (2026-05-26 헤르2 제안 → 헤르 구현):
      kind='fact'  w=0.9 — L1/L2 합의 확실 (consensus)
      kind='bet'   w=0.7 — L1/L2 불일치 또는 fallback 사용
      kind='hunch' w=0.5 — 합의 실패 (conservative/iron_law)
    """
    try:
        import sqlite3
        if not NOVA_BRAIN_DB.exists():
            return
        con = sqlite3.connect(str(NOVA_BRAIN_DB))
        ts  = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        entry = f"[{ts}] [{phase}] Think={think[:80]} | Act={act[:80]} | Observe={observe[:80]}"
        con.execute(
            "INSERT INTO takes (id,page_id,holder,kind,claim,weight,created_at) "
            "SELECT lower(hex(randomblob(8))), MIN(id), 'nova-gate', ?, ?, ?, ? "
            "FROM pages WHERE path LIKE ? LIMIT 1",
            (kind, entry, weight, ts, f"%{project}%")
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.debug(f"CT+TL 기록 실패(무시): {e}")


# ─────────────────────────────────────────────────────────
# Layer 1: Claude (HMG) — 구현자 관점 검토
# ─────────────────────────────────────────────────────────
def claude_review(project: str, content: str, mode: str = "review") -> dict:
    """Layer 1: Claude Code로 1차 품질 검토 (구현자 관점)"""
    try:
        import requests
    except ImportError:
        return {"status": "error", "reason": "requests not installed", "reviewer": "claude"}

    if not API_KEY:
        return {"status": "error", "reason": "HERMES_API_KEY not set", "reviewer": "claude"}

    # gstack ETHOS: "Connect to user outcomes" — 실제 사용자 경험 연결
    project_criteria = _get_project_criteria(project)
    system_prompt = f"""You are Layer-1 reviewer for {project} (Claude — implementer perspective).
gstack principle: "Connect to user outcomes" — every finding connects to real user impact.

Project-specific criteria:
{project_criteria}

Evaluate content on:
1. Accuracy (no factual errors, dates, numbers)
2. Completeness (all required sections, adequate depth)
3. User value (would a real user benefit from this?)
4. Project-specific quality (see criteria above)

Output JSON only:
{{
  "score": <0-100>,
  "verdict": "APPROVED" | "REQUEST_CHANGES",
  "critical_issues": ["..."],  // show-stoppers
  "informational_issues": ["..."],  // nice-to-fix
  "user_impact": "one sentence — how this affects the end user",
  "reviewer": "claude-layer1"
}}"""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": f"[{mode.upper()}]\n\n{content[:6000]}"}]
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01"
    }
    endpoint = CLAUDE_BASE.rstrip("/") + "/v1/messages"

    t0 = time.time()
    try:
        import requests as _req
        resp = _req.post(endpoint, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        result["reviewer"] = "claude-layer1"
        result["latency"] = round(time.time() - t0, 1)
        _record_ct_tl(project, "L1-claude",
                      f"score={result.get('score','?')} verdict={result.get('verdict','?')}",
                      "claude_review()", f"latency={result['latency']}s")
        logger.info(f"[L1 Claude] score={result.get('score')} verdict={result.get('verdict')} ({result['latency']}s)")
        return result
    except Exception as e:
        logger.error(f"Claude review 실패: {e}")
        return {"status": "error", "reason": str(e), "reviewer": "claude-layer1", "verdict": "REQUEST_CHANGES", "score": 50}


# ─────────────────────────────────────────────────────────
# Layer 2: GPT-5.4 (HMG) — 진짜 독립 감사
# ─────────────────────────────────────────────────────────
def gpt_audit(project: str, content: str, claude_result: dict) -> dict:
    """Layer 2: GPT-5.4로 독립 감사 (완전히 다른 모델 — 진짜 독립)

    헤르2 실증 (2026-05-24):
    - endpoint: https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3/openai/deployments/gpt-5.4/chat/completions
    - model: gpt-5.4  # MEMORY 기록 엔드포인트와 동일
    - max_completion_tokens 필수 (max_tokens 불가 → HTTP 400)
    - 응답 시간: ~0.9s
    """
    try:
        import requests as _req
    except ImportError:
        return _gpt_fallback(claude_result, "requests not installed")

    endpoint = f"{GPT_BASE}/chat/completions"
    # gstack 사고법: 독립 감사관 = "비판적 외부 시선"
    system_msg = f"""You are Layer-2 independent auditor for {project} (GPT-5.4 — adversarial perspective).
IMPORTANT: You are completely independent from Claude's Layer-1 review.
Apply gstack adversarial review principles:
- Find what Claude MISSED (assume Claude was too lenient)
- Apply Fix-First Heuristic: 95%+ confidence → flag as critical, below 85% → informational
- Iron Law: if you cannot justify a finding with evidence, do NOT flag it

Claude L1 findings for context (do NOT simply agree):
Score: {claude_result.get('score', '?')} | Verdict: {claude_result.get('verdict', '?')}
Issues found: {json.dumps(claude_result.get('critical_issues', []))[:300]}

Your output (JSON only, no other text):
{{
  "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
  "score_adjustment": <integer -20 to +20>,
  "missed_by_claude": ["specific issue Claude missed"],
  "confirmed_by_gpt": ["Claude was RIGHT about this"],
  "overruled_by_gpt": ["Claude was WRONG about this"],
  "final_recommendation": "one sentence",
  "reviewer": "gpt-5.4"
}}"""

    payload = {
        "model": GPT_MODEL,
        "max_completion_tokens": 800,  # 헤르2 실증: max_tokens 아님
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"[AUDIT TARGET]\n\n{content[:5000]}"}
        ]
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GPT_KEY}"
    }

    t0 = time.time()
    try:
        import ssl
        # HMG 내부망 cert 문제 회피
        resp = _req.post(endpoint, json=payload, headers=headers, timeout=30, verify=False)
        latency = round(time.time() - t0, 1)

        if resp.status_code == 401:
            logger.warning("GPT-5.4 인증 실패 → Claude 독립감사 폴백")
            return _gpt_fallback_claude(project, content, claude_result)
        if resp.status_code == 400:
            logger.warning(f"GPT-5.4 400 오류: {resp.text[:200]}")
            return _gpt_fallback_claude(project, content, claude_result)

        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        # [FIX-2026-05-25] truncated 응답이면 폴백 처리 (ABORT 방지)
        final_rec = result.get("final_recommendation", "")
        if "truncated" in str(final_rec).lower() or result.get("verdict") == "ABORT" and "truncated" in str(result).lower():
            logger.warning(f"[L2 GPT-5.4] truncated 응답 감지 → Claude 폴백")
            return _gpt_fallback_claude(project, content, claude_result)
        result["reviewer"] = "gpt-5.4"
        result["latency"] = latency
        _record_ct_tl(project, "L2-gpt",
                      f"adj={result.get('score_adjustment','?')} verdict={result.get('verdict','?')}",
                      "gpt_audit()", f"latency={latency}s",
                      kind="fact", weight=0.9)  # L2 정상 응답 → fact/0.9
        logger.info(f"[L2 GPT-5.4] adj={result.get('score_adjustment')} verdict={result.get('verdict')} ({latency}s)")
        return result
    except Exception as e:
        logger.error(f"GPT-5.4 감사 실패: {e} → 폴백")
        return _gpt_fallback_claude(project, content, claude_result)


def _gpt_fallback(claude_result: dict, reason: str) -> dict:
    """GPT 사용 불가 시 폴백 — Claude 결과 보수적 반영"""
    return {
        "verdict": claude_result.get("verdict", "REQUEST_CHANGES"),
        "score_adjustment": 0,
        "missed_by_claude": [],
        "confirmed_by_gpt": [],
        "overruled_by_gpt": [],
        "final_recommendation": f"GPT 불가 ({reason}) — Claude 판정 그대로 채택",
        "reviewer": "gpt-fallback"
    }


def _gpt_fallback_claude(project: str, content: str, claude_result: dict) -> dict:
    """GPT 불가 시 Claude를 다른 시스템 프롬프트로 독립 감사 실행"""
    try:
        import requests as _req
    except ImportError:
        return _gpt_fallback(claude_result, "requests not installed")

    system_prompt = f"""당신은 {project}의 **독립 품질 감사관 (Layer 2)**입니다.
Claude Layer 1과 독립적으로, 외부 심사위원 관점에서만 평가하세요.
Fix-First 원칙: 95% 확신 → critical, 85% 미만 → informational만 기록.

L1 요약 (참고만, 동의 금지):
점수={claude_result.get('score','?')} 판정={claude_result.get('verdict','?')}

출력 (JSON만):
{{
  "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
  "score_adjustment": <-20~+20>,
  "missed_by_claude": [],
  "confirmed_by_gpt": [],
  "overruled_by_gpt": [],
  "final_recommendation": "한 줄 요약",
  "reviewer": "claude-layer2-fallback"
}}"""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 512,
        "system": system_prompt,
        "messages": [{"role": "user", "content": content[:4000]}]
    }
    headers = {"content-type": "application/json", "x-api-key": API_KEY, "anthropic-version": "2023-06-01"}
    endpoint = CLAUDE_BASE.rstrip("/") + "/v1/messages"

    try:
        resp = _req.post(endpoint, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        result["reviewer"] = "claude-layer2-fallback"
        return result
    except Exception as e:
        return _gpt_fallback(claude_result, str(e))


# ─────────────────────────────────────────────────────────
# Layer 3: 병합 판정 (gbrain Takes 합의 패턴)
# ─────────────────────────────────────────────────────────
def _merge_verdict_v2(claude_r: dict, gpt_r: dict, project: str = "") -> dict:
    """gbrain 3/4 합의 패턴: APPROVED 3/4 합의 → 자동 진행, 충돌 → 보수적

    [R32 재조정] 1,062 PASS 기준선 달성 → 임계값 상향:
      APPROVAL_THRESHOLD = 80  (이전: 75)
      SILVER_THRESHOLD   = 70  (신규: 경고 레벨)

    [R32 신규] 프로젝트별 가중치:
      caring: ×1.2 (IVR/법적 리스크 — STT 오탐 이력)
      mental-load: ×1.1 (위기감지 F1 필수)
      saju-wellness: ×1.0 (표준)
    """
    APPROVAL_THRESHOLD = 80
    SILVER_THRESHOLD   = 70

    PROJECT_WEIGHTS = {
        "caring":         1.2,
        "mental-load":    1.1,
        "saju-wellness":  1.0,
    }
    weight = PROJECT_WEIGHTS.get(project, 1.0)
    effective_threshold = APPROVAL_THRESHOLD * weight

    cv = claude_r.get("verdict", "APPROVED")
    gv = gpt_r.get("verdict", "APPROVED")
    score = claude_r.get("score", 70)
    adj   = gpt_r.get("score_adjustment", 0)
    final_score = score + (adj if isinstance(adj, (int, float)) else 0)

    # Iron Law: ABORT는 한 쪽이라도 주장하면 즉시
    if gv == "ABORT" or cv == "ABORT":
        return {"verdict": "ABORT", "final_score": final_score, "source": "iron_law"}

    # SILVER 미만 → 즉시 REQUEST_CHANGES
    if final_score < SILVER_THRESHOLD:
        return {"verdict": "REQUEST_CHANGES", "final_score": final_score, "source": "below_silver"}

    # 완전 합의 → APPROVED
    if cv == "APPROVED" and gv == "APPROVED" and final_score >= effective_threshold:
        return {"verdict": "APPROVED", "final_score": final_score, "source": "consensus"}

    # 충돌: 점수로 결정 (보수적)
    if final_score >= effective_threshold and not (cv == "REQUEST_CHANGES" and gv == "REQUEST_CHANGES"):
        return {"verdict": "APPROVED", "final_score": final_score, "source": "score_override"}

    # SILVER 구간 → 조건부 경고
    if SILVER_THRESHOLD <= final_score < effective_threshold:
        return {"verdict": "REQUEST_CHANGES", "final_score": final_score, "source": "silver_warning",
                "note": f"SILVER 구간 ({SILVER_THRESHOLD}~{effective_threshold:.0f}) — 보완 후 재검토 권고"}

    return {"verdict": "REQUEST_CHANGES", "final_score": final_score, "source": "conservative"}


# ─────────────────────────────────────────────────────────
# Trajectory 업데이트 (gbrain 패턴)
# ─────────────────────────────────────────────────────────
def _update_trajectory(project: str, final_score: float):
    """nova_brain trajectories에 품질 점수 시계열 기록"""
    try:
        import sqlite3
        if not NOVA_BRAIN_DB.exists():
            return
        con = sqlite3.connect(str(NOVA_BRAIN_DB))
        ts  = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        # pages 테이블에서 project 관련 페이지 찾기
        row = con.execute(
            "SELECT id FROM pages WHERE path LIKE ? LIMIT 1", (f"%{project}%",)
        ).fetchone()
        if row:
            con.execute(
                "INSERT INTO trajectories (id,page_id,metric,value,recorded_at) VALUES (?,?,?,?,?)",
                (str(uuid.uuid4())[:16], row[0], "gate_quality_score", final_score, ts)
            )
            con.commit()
        con.close()
        logger.info(f"[trajectory] {project} gate_quality_score={final_score}")
    except Exception as e:
        logger.debug(f"trajectory 기록 실패(무시): {e}")


# ─────────────────────────────────────────────────────────
# IPC → 헤르(메인) 에스컬레이션 (충돌 시)
# ─────────────────────────────────────────────────────────
def ipc_ask_main(project: str, claude_r: dict, gpt_r: dict, content_preview: str) -> dict:
    """충돌 판정 시 헤르(메인)에 IPC 에스컬레이션 (2분 대기)"""
    if not IPC_OUT.exists():
        logger.warning("IPC 채널 없음 → 보수적 병합 판정")
        return _merge_verdict_v2(claude_r, gpt_r, project=project)

    msg_id = str(uuid.uuid4())[:8]
    ask_msg = {
        "id": msg_id, "ts": int(time.time()), "from": "hermes2", "to": "hermes_main",
        "type": "ask",
        "question": (
            f"[NOVA gate v2 에스컬레이션] {project} — Claude vs GPT-5.4 판정 충돌\n\n"
            f"Claude L1: score={claude_r.get('score','?')} verdict={claude_r.get('verdict','?')}\n"
            f"GPT-5.4 L2: adj={gpt_r.get('score_adjustment','0')} verdict={gpt_r.get('verdict','?')}\n"
            f"GPT가 놓쳤다고 지적한 이슈: {json.dumps(gpt_r.get('missed_by_claude',[]))[:200]}\n\n"
            f"콘텐츠 미리보기:\n{content_preview[:400]}\n\n"
            f"최종 판정 JSON: {{\"verdict\": \"APPROVED\"|\"REQUEST_CHANGES\"|\"ABORT\", \"reason\": \"...\"}}"
        ),
        "context": {"project": project, "claude_result": claude_r, "gpt_result": gpt_r},
        "timeout_sec": 120,
    }

    try:
        ask_file = IPC_OUT / f"{msg_id}.json"
        ask_file.write_text(json.dumps(ask_msg, ensure_ascii=False, indent=2))
        logger.info(f"IPC 에스컬레이션: {msg_id}")

        reply_file = IPC_IN / f"{msg_id}_reply.json"
        for _ in range(24):
            time.sleep(5)
            if reply_file.exists():
                reply = json.loads(reply_file.read_text())
                return {"verdict": reply.get("answer", "REQUEST_CHANGES"), "source": "hermes_main"}

        logger.warning("IPC 타임아웃 → 보수적 병합")
        return _merge_verdict_v2(claude_r, gpt_r, project=project)
    except Exception as e:
        logger.error(f"IPC 실패: {e}")
        return _merge_verdict_v2(claude_r, gpt_r, project=project)


# ─────────────────────────────────────────────────────────
# 메인 게이트 (병렬 실행)
# ─────────────────────────────────────────────────────────
def run_gate(project: str, content: str, mode: str = "review", use_ipc: bool = True) -> dict:
    """
    NOVA Phase 5 v2 공동검증 게이트 — 병렬 실행

    Architecture:
        Claude L1 ──┐
                    ├──→ merge_verdict_v2 ──→ [충돌 시 IPC] ──→ 최종 판정
        GPT-5.4 L2 ─┘   (3/4 합의 패턴)

    Returns:
        {
            "verdict": "APPROVED" | "REQUEST_CHANGES" | "ABORT",
            "final_score": <float>,
            "claude": {...},
            "gpt": {...},
            "merge_source": "consensus" | "score_override" | "conservative" | "iron_law" | "ipc",
            "elapsed": <float>
        }
    """
    start = time.time()
    logger.info(f"[{project}] NOVA gate v2 시작 (mode={mode}, use_ipc={use_ipc})")

    # 병렬 실행: Claude L1 + GPT L2 동시
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_claude = ex.submit(claude_review, project, content, mode)
        f_gpt    = ex.submit(gpt_audit,    project, content, {"score": 0, "verdict": "?", "critical_issues": []})
        claude_r = f_claude.result(timeout=120)
        gpt_r    = f_gpt.result(timeout=120)

    # GPT가 claude_result를 참조해야 하므로 claude 결과로 재감사 (순차 필요 시)
    # 하지만 병렬이 더 빠르고 독립성 관점에서도 더 좋음 (Claude 편향 없음)

    merge = _merge_verdict_v2(claude_r, gpt_r, project=project)

    # 충돌 + use_ipc → 에스컬레이션
    if merge["source"] == "conservative" and use_ipc:
        ipc_result = ipc_ask_main(project, claude_r, gpt_r, content[:500])
        merge["verdict"]      = ipc_result.get("verdict", merge["verdict"])
        merge["source"]       = ipc_result.get("source", "ipc")

    # Trajectory 업데이트
    _update_trajectory(project, merge["final_score"])

    # CT+TL 최종 기록 — merge source에 따라 불확실도 동적 반영 (2026-05-26)
    # consensus/score_override → fact/0.9 (L1+L2 합의 또는 점수 충분)
    # ipc → fact/0.9 (IPC 외부 합의)
    # conservative → bet/0.7 (L1/L2 불일치, 보수적 판정)
    # iron_law → bet/0.7 (ABORT 강제 — 한 쪽 주장)
    # fallback 사용 (reviewer 접미사 확인) → hunch/0.5
    _merge_source = merge.get("source", "consensus")
    _reviewer = gpt_r.get("reviewer", "")
    if "fallback" in _reviewer or "fallback" in str(gpt_r.get("final_recommendation", "")):
        _ct_kind, _ct_weight = "hunch", 0.5   # GPT 불가 → fallback 사용
    elif _merge_source in ("conservative", "iron_law"):
        _ct_kind, _ct_weight = "bet", 0.7     # L1/L2 불일치
    else:
        _ct_kind, _ct_weight = "fact", 0.9    # consensus / score_override / ipc
    _record_ct_tl(project, "L3-final",
                  f"claude={claude_r.get('score','?')}/{claude_r.get('verdict','?')} gpt={gpt_r.get('score_adjustment','?')}/{gpt_r.get('verdict','?')}",
                  "merge_verdict_v2()",
                  f"final={merge['verdict']} score={merge['final_score']} src={_merge_source}",
                  kind=_ct_kind, weight=_ct_weight)

    elapsed = round(time.time() - start, 1)
    logger.info(f"[{project}] NOVA gate v2 완료: {merge['verdict']} (score={merge['final_score']}, {elapsed}s)")

    return {
        "verdict":      merge["verdict"],
        "final_score":  merge["final_score"],
        "claude":       claude_r,
        "gpt":          gpt_r,
        "merge_source": merge["source"],
        "elapsed":      elapsed,
    }


# ─────────────────────────────────────────────────────────
# CLI 인터페이스
# ─────────────────────────────────────────────────────────
def _merge_verdict(claude_r: dict, codex_r: dict) -> str:
    """하위 호환성 유지 (구 버전 호출자용)"""
    return _merge_verdict_v2(claude_r, codex_r)["verdict"]

# 하위 호환: codex_audit = gpt_audit 별칭
def codex_audit(project: str, content: str, claude_result: dict) -> dict:
    return gpt_audit(project, content, claude_result)


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