#!/usr/bin/env python3
"""nova_brain_watchdog.py v4 — 헤르 서버 전용
nova_brain.db 상태 임계값 모니터링 + Telegram 알림

임계값:
  - takes 2일 연속 증가 없음 → "자율 루프 정지 의심" 알림
  - pages 3일 연속 증가 없음 → "synthesize 크론 이상" 알림
  - open_contradictions 3건 이상 → "모순 누적" 알림
  - health_score 95.0 미만 → "DB 건강 이상" 알림

크론: 매일 06:00 KST (UTC 21:00)
  0 21 * * * python3 $HERMES_HOME/scripts/nova_brain_watchdog.py
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import urllib.request
import ssl
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))))
DB_PATH     = HERMES_HOME / "nova_brain.db"
STATE_FILE  = HERMES_HOME / "scripts" / "nova_watchdog_state.json"
ENV_FILE    = HERMES_HOME / ".env"
LOG_FILE    = HERMES_HOME / "logs" / "nova_brain_watchdog.log"

TG_CHAT_ID  = -1003957968994
TG_THREAD_ID = 9  # 헤르운영

# ── 환경변수 로드 ──────────────────────────────────────
def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


# ── DB 실측 ──────────────────────────────────────────
def query_db() -> dict:
    if not DB_PATH.exists():
        return {}
    db = sqlite3.connect(str(DB_PATH))
    c  = db.cursor()

    def count(table: str, where: str = "1=1") -> int:
        try:
            c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}")
            return c.fetchone()[0]
        except Exception:
            return -1

    takes  = count("takes")
    pages  = count("pages")
    open_c = count("contradictions", "status='open'")

    # brain_health 테이블 — 헤르 서버 전용 (score_overall 컬럼)
    health_score = 100.0
    try:
        c.execute("SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1")
        row = c.fetchone()
        if row and row[0] is not None:
            health_score = float(row[0])
    except Exception:
        # fallback: pages.health_score 평균
        try:
            c.execute("SELECT AVG(health_score) FROM pages WHERE health_score IS NOT NULL")
            row = c.fetchone()
            if row and row[0]:
                health_score = float(row[0])
        except Exception:
            pass

    db.close()
    return {
        "takes": takes,
        "pages": pages,
        "open_contradictions": open_c,
        "health_score": health_score,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ── 상태 저장/로드 ──────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"history": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── Telegram 알림 ─────────────────────────────────
def send_alert(token: str, text: str) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {
        "chat_id": TG_CHAT_ID,
        "message_thread_id": TG_THREAD_ID,
        "text": text,
    }
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as e:
        log(f"Telegram 전송 실패: {e}")
        return False


# ── 로그 ─────────────────────────────────────────
def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}\n"
    with LOG_FILE.open("a") as f:
        f.write(line)
    print(msg)


# ── 임계값 체크 ──────────────────────────────────
def check_thresholds(history: list[dict], current: dict) -> list[str]:
    alerts: list[str] = []

    # takes 2일 연속 증가 없음
    if len(history) >= 2:
        prev2 = history[-2]["takes"]
        prev1 = history[-1]["takes"]
        curr  = current["takes"]
        if curr <= prev1 and prev1 <= prev2:
            alerts.append(
                f"⚠️ [nova_watchdog] takes 2일 연속 증가 없음\n"
                f"  D-2={prev2} / D-1={prev1} / 현재={curr}\n"
                f"  → 자율 루프 정지 의심"
            )

    # pages 3일 연속 증가 없음
    if len(history) >= 3:
        vals = [h["pages"] for h in history[-3:]] + [current["pages"]]
        if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
            alerts.append(
                f"⚠️ [nova_watchdog] pages 3일 연속 증가 없음\n"
                f"  {vals}\n"
                f"  → nova_synthesize 크론 이상 의심"
            )

    # open_contradictions 3건 이상
    open_c = current.get("open_contradictions", 0)
    if open_c >= 3:
        alerts.append(
            f"⚠️ [nova_watchdog] open_contradictions={open_c}건\n"
            f"  → 모순 누적, 수동 dismiss 검토 필요"
        )

    # health_score 95 미만
    score = current.get("health_score", 100.0)
    if score < 95.0:
        alerts.append(
            f"⚠️ [nova_watchdog] health_score={score:.1f} (임계값 95.0)\n"
            f"  → DB 건강 이상"
        )

    return alerts


# ── 메인 ─────────────────────────────────────────
def main() -> None:
    log("nova_brain_watchdog v4 시작")
    env   = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_TOKEN", "")
    if not token:
        for k, v in env.items():
            if "TOKEN" in k.upper() and "TELEGRAM" in k.upper():
                token = v
                break

    current = query_db()
    if not current:
        log("nova_brain.db 없음 — 스킵")
        return

    log(f"실측: takes={current['takes']} / pages={current['pages']} / "
        f"open={current['open_contradictions']} / score={current['health_score']:.1f}")

    state   = load_state()
    history = state.get("history", [])
    alerts  = check_thresholds(history, current)

    if alerts:
        for alert in alerts:
            log(f"ALERT: {alert}")
            if token:
                send_alert(token, alert)
    else:
        log("SILENT — 모든 임계값 정상")

    # 히스토리 저장 (최근 7일)
    history.append(current)
    state["history"] = history[-7:]
    save_state(state)
    log("nova_brain_watchdog 완료")


if __name__ == "__main__":
    main()
