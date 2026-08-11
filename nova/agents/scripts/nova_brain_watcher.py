#!/usr/bin/env python3
"""
nova_brain_watcher.py v2 — 두뇌 기억 변화 감지 (1초 폴링)
=============================================================
원칙:
  "상태 기반 판단" — 주기가 아닌 변화가 핵심
  1초마다 nova_brain.db 스냅샷 비교
  변화 없으면 SILENT / 변화 있으면 즉시 반응

두뇌(헤르)의 기억(nova_brain.db)이 바뀌는 순간:
  takes 증가 → 새 지식 축적 → synthesize/learn/dream 판단
  orphan 발생 → 즉시 정리
  contradictions 발생 → 즉시 헤르에게 알림
  health 하락 → 즉시 DreamCycle
  kanban 변화 → 즉시 chain_engine
"""

import sqlite3, time, os, json, uuid, datetime, subprocess
from pathlib import Path

_HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))

DB          = f"{_HERMES_HOME}/nova_brain.db"
KANBAN_DB   = f"{_HERMES_HOME}/kanban/boards/rail-saas/kanban.db"
STATE_FILE  = f"{_HERMES_HOME}/logs/nova_brain_watcher_state.json"
LOG_FILE    = f"{_HERMES_HOME}/logs/nova_brain_watcher.log"
SCRIPTS     = f"{_HERMES_HOME}/scripts"
MEMORY_MD   = Path.home() / ".hermes" / "memories" / "MEMORY.md"
MEMORY_LIMIT = 20000
MEMORY_SLIM  = Path(SCRIPTS) / "memory_slim.py"

# 외부 성장 신호 수집 — cron 없이 brain event에 편승
RESOURCE_UPDATER   = Path(SCRIPTS) / "nova_resource_updater.py"
RESOURCE_MIN_GAP_S = 6 * 3600   # synthesize/dream 반응 시 마지막 수집 후 6h 경과해야 실행

# nova_resource_collector — 대규모 수집 (주 1회 이상 금지, dream+takes급 이벤트에만)
RESOURCE_COLLECTOR  = Path(SCRIPTS) / "nova_resource_collector.py"
COLLECTOR_MIN_GAP_S = 7 * 24 * 3600  # 7일 간격 — nova-resource-seo/marketing/dev-weekly 대체

AUDIT_LOOP_SH      = Path(SCRIPTS) / "nova_audit_loop.sh"
AUDIT_MIN_GAP_S    = 12 * 3600  # STAGNANT/health 이벤트 시 마지막 감사 후 12h 경과해야 실행

# blog_geo_engine — 발행 훅 감지 + 일일 분석
GEO_ENGINE         = Path(SCRIPTS) / "blog_geo_engine.py"
GEO_ENGINE_MIN_S   = 300   # BLOG_PUBLISHED 이벤트 연속 발행 시 최소 간격 5분
GEO_DAILY_MIN_S    = 20 * 3600  # 일일 전체 분석 최소 간격 20h

# nova_wiki_synthesize — crosslink/stale/takes phase 편승
WIKI_SYNTH    = Path(_HERMES_HOME) / "bin/nova_wiki_synthesize.py"
WIKI_CROSSLINK_MIN_GAP_S = 6 * 3600    # synthesize 반응 시 편승 (6h 간격)
WIKI_STALE_MIN_GAP_S     = 24 * 3600   # dream 반응 후 편승 (1일 간격 — 무거운 LLM 재생성)
WIKI_TAKES_MIN_GAP_S     = 12 * 3600   # takes +100(dream급) 반응 후 편승 (12h 간격)

REACT = {
    "takes_for_dream":      100,    # brain: +100개 → DreamCycle (50→30→100: nova-chain 7.5/min 고려)
    "takes_for_synthesize": 15,    # brain: +15개 → synthesize (20→15 완화)
    "takes_for_learn":       5,    # brain: +5개 → learn_engine
    "orphan_max":            3,    # orphan ≥ 3 → 즉시 정리
    "health_critical":      90.0,  # health < 90 → DreamCycle
    "chain_min_s":          10,    # chain_engine 최소 간격(초)
    "synthesize_min_s":    300,    # synthesize 최소 간격(초)
    "dream_min_s":        7200,    # DreamCycle 최소 간격(초) 1h→2h (nova-chain 과다 트리거 방지)
    "learn_min_s":        1800,    # learn_engine 최소 간격(초)
}

Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[brain-watcher] [{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def snap_brain():
    try:
        db = sqlite3.connect(DB, timeout=2)
        c = db.cursor()
        takes = c.execute("SELECT count(*) FROM takes").fetchone()[0]
        orphan = c.execute("SELECT count(*) FROM pages WHERE agent IS NULL AND page_type='general'").fetchone()[0]
        open_c = c.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
        health = (c.execute("SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1").fetchone() or [100.0])[0]
        db.close()
        return {"takes": takes, "orphan": orphan, "open_contra": open_c, "health": health}
    except Exception:
        return None


NOVA_BOARDS_JSON = f"{_HERMES_HOME}/kanban/nova_boards.json"  # BUG-M4 fix: KANBAN_DB 중복 선언 제거 (line 22의 것 사용)


def snap_kanban():
    """모든 등록된 NOVA 보드의 합산 스냅샷"""
    try:
        import json as _json
        boards_file = Path(NOVA_BOARDS_JSON)
        boards = _json.load(open(boards_file))["boards"] if boards_file.exists() else ["rail-saas"]
        total_done = 0
        total_active = 0
        for board in boards:
            db_path = f"{_HERMES_HOME}/kanban/boards/{board}/kanban.db"
            if not Path(db_path).exists():
                continue
            try:
                db = sqlite3.connect(db_path, timeout=2)
                c = db.cursor()
                done = c.execute("SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0]
                active = c.execute("SELECT count(*) FROM tasks WHERE status IN ('running','todo','ready')").fetchone()[0]
                db.close()
                total_done += done
                total_active += active
            except Exception:
                pass
        return {"done": total_done, "active": total_active}
    except Exception:
        return None


def snap_kb():
    """KB 파일 변화 감지 — 파일 수 + 최신 mtime (다중 경로)
    wiki/는 제외 — nova_kb_sync가 재인덱싱 시 wiki mtime 변경 → 순환 트리거 방지
    """
    try:
        hermes_home = Path(_HERMES_HOME)
        scan_roots = [
            hermes_home / "kb",
            # wiki/는 스캔 제외: kb_sync가 wiki 재인덱싱 시 mtime 변경 → 순환 감지 방지
            # hermes_home / "wiki",
            hermes_home / "doosi" / "kb",
        ]
        md_files = []
        for root in scan_roots:
            if root.exists():
                md_files.extend(root.rglob("*.md"))
        count = len(md_files)
        latest_mtime = max((f.stat().st_mtime for f in md_files), default=0)
        return {"count": count, "latest_mtime": round(latest_mtime, 1)}
    except Exception:
        return None


def can_act(state, key, min_s):
    return (time.time() - state.get(f"last_{key}", 0)) >= min_s


def act(state, key, fn, *args):
    fn(*args)
    state[f"last_{key}"] = time.time()


def run(script, timeout=300):
    path = f"{SCRIPTS}/{script}"
    if not Path(path).exists():
        return False
    try:
        r = subprocess.run(
            ["bash", path] if script.endswith(".sh") else ["python3", path],
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode == 0
    except Exception:
        return False


def snap_memory() -> dict:
    """MEMORY.md 사용률 스냅샷"""
    try:
        if MEMORY_MD.exists():
            chars = len(MEMORY_MD.read_text(encoding="utf-8"))
            return {"chars": chars, "pct": int(chars * 100 / MEMORY_LIMIT)}
    except Exception:
        pass
    return {"chars": 0, "pct": 0}


def run_memory_slim() -> bool:
    """memory_slim.py 실행 — 85%+ 시 자동 슬림화"""
    if not MEMORY_SLIM.exists():
        return False
    try:
        r = subprocess.run(
            ["python3", str(MEMORY_SLIM), "--force"],
            capture_output=True, text=True, timeout=60
        )
        return r.returncode == 0
    except Exception as e:
        log(f"  [MEMORY-SLIM-ERR] {e}")
        return False


def push_event(evt_type, severity, title, detail=""):
    db = None
    try:
        db = sqlite3.connect(DB, timeout=2)
        c = db.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        eid = uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO hermes_events (id,event_type,severity,title,detail,source_agent,created_at,is_read) VALUES (?,?,?,?,?,?,?,?)",
            (eid, evt_type, severity, title, detail, "nova_brain_watcher", now, 0)
        )
        db.commit()
    except Exception as e:
        log(f"  [PUSH-EVENT-ERR] {e}")
    finally:
        if db:
            db.close()


def detect_stagnant_agents() -> list[dict]:
    """STAGNANT 에이전트 감지 — 최신 200개 takes 기준 hq_ratio < 15%
    R16 정밀감사: 전체 takes 기준 → 최신 200개 샘플 기준으로 수정
    """
    stagnant = []
    try:
        db = sqlite3.connect(DB, timeout=3)
        c = db.cursor()
        # 30개 이상 takes 보유 에이전트 목록
        agents = c.execute(
            "SELECT holder, count(*) FROM takes GROUP BY holder HAVING count(*)>=30"
        ).fetchall()
        for agent, total in agents:
            # 최신 200개 샘플 기준 hq_ratio 계산
            rows = c.execute(
                "SELECT weight FROM takes WHERE holder=? ORDER BY created_at DESC LIMIT 200",
                (agent,)
            ).fetchall()
            sample = [r[0] for r in rows]
            if not sample:
                continue
            hq = sum(1 for w in sample if w >= 0.85)
            hq_ratio = hq / len(sample) * 100
            if hq_ratio < 15.0:
                stagnant.append({
                    "agent": agent, "total": total,
                    "sample_n": len(sample), "hq_ratio": hq_ratio
                })
        db.close()
    except Exception as e:
        log(f"  [STAGNANT-DETECT-ERR] {e}")
    return stagnant


def fix_orphans():
    db = None
    try:
        db = sqlite3.connect(DB, timeout=2)
        c = db.cursor()
        orphans = c.execute("SELECT id FROM pages WHERE agent IS NULL AND page_type='general'").fetchall()
        for (pid,) in orphans:
            c.execute("UPDATE pages SET agent='nova-evaluator' WHERE id=?", (pid,))
        db.commit()
        log(f"  orphan {len(orphans)}개 정리")
    except Exception as e:
        log(f"  [FIX-ORPHAN-ERR] {e}")
    finally:
        if db:
            db.close()  # Codex LOW BUG fix: try/finally 보장



import re as _re

_CHAIN_PAT = _re.compile(
    r'\[chain\]\s+(\S+):\s+(\S+?)→(\S+?)\s+\(\w+\)\s+task=(\S+)'
)


def _score_page(claim_words: set, path: str, char_count: int, page_type: str) -> float:
    """
    claim 키워드 ↔ page path/type 기반 점수 계산 (0~1 범위).

    규칙:
      - path에 claim 단어가 포함될수록 높은 점수 (최대 0.5)
      - char_count가 클수록 내용 풍부 (최대 0.3, log-scale)
      - page_type=='agent' 이면 보너스 0.1
      - page_type=='general' 이면 페널티 -0.05
    """
    import math
    score = 0.0

    # 1) 키워드 overlap: path를 소문자 토큰으로 분해
    path_tokens = set(_re.split(r'[/\-_.\s]+', path.lower()))
    overlap = len(claim_words & path_tokens)
    score += min(overlap / max(len(claim_words), 1), 1.0) * 0.5

    # 2) char_count 가중치 (내용이 풍부한 페이지 선호)
    if char_count and char_count > 0:
        score += min(math.log1p(char_count) / math.log1p(20000), 1.0) * 0.3

    # 3) page_type 보너스/페널티
    if page_type == "agent":
        score += 0.1
    elif page_type == "general":
        score -= 0.05

    return score


def auto_link_takes_to_kb():
    """
    page_id 없는 takes를 관련 KB 페이지에 자동 연결 (v2 — 랭킹 기반).

    개선 내용 (2026-06-08):
      - nova-chain holder: claim 파싱(board+agent) → board/agent path 우선 매칭
      - 일반 nova-* holder: 키워드 스코어링 (_score_page) → best-rank 선택
      - skill_kb_bridge 등 기타: claim 키워드 기반 전체 pages 스코어링
      - Blind pages[0] 제거 → 항상 ranked best match 사용
    """
    try:
        db = sqlite3.connect(DB, timeout=2)
        c = db.cursor()

        # page_id 없는 최근 takes (최대 100개)
        orphan_takes = c.execute("""
            SELECT id, claim, holder FROM takes
            WHERE page_id IS NULL AND claim != ''
            ORDER BY rowid DESC LIMIT 100
        """).fetchall()

        if not orphan_takes:
            db.close()
            return 0

        linked = 0
        for take_id, claim, holder in orphan_takes:
            best_page_id = None

            # ── 전략 A: nova-chain claim 파싱 ──────────────────────────────
            if holder == "nova-chain":
                m = _CHAIN_PAT.match(claim)
                if m:
                    board = m.group(1)       # e.g. "rail-saas"
                    to_agent = m.group(3)    # e.g. "nova-cso"

                    # 우선순위: board+agent path > agent 전용 index 페이지
                    candidates = c.execute("""
                        SELECT id, path, char_count, page_type FROM pages
                        WHERE path != ''
                          AND (
                            (path LIKE ? AND path LIKE ?)
                            OR path LIKE ?
                          )
                        ORDER BY
                          CASE WHEN path LIKE ? THEN 0 ELSE 1 END,
                          char_count DESC
                        LIMIT 10
                    """, (
                        f"%{board}%",
                        f"%{to_agent}%",
                        f"agents/{to_agent}/%",
                        f"%{board}%{to_agent}%",
                    )).fetchall()

                    if candidates:
                        claim_words = set(
                            _re.split(r'[\s\-_/]+', claim.lower())
                        ) - {"", "chain", "forward", "backward", "task"}
                        scored = [
                            (_score_page(claim_words, p, ch, pt), pid)
                            for pid, p, ch, pt in candidates
                        ]
                        scored.sort(key=lambda x: -x[0])
                        best_page_id = scored[0][1]

            # ── 전략 B: 일반 nova-* agent holder ───────────────────────────
            elif holder and holder.startswith("nova-"):
                agent = holder
                candidates = c.execute("""
                    SELECT id, path, char_count, page_type FROM pages
                    WHERE path LIKE ?
                      AND path != ''
                    LIMIT 20
                """, (f"agents/{agent}/%",)).fetchall()

                if candidates:
                    claim_words = set(
                        _re.split(r'[\s\-_/:.]+', claim.lower())
                    ) - {"", "the", "and", "or", "is", "a", "in", "of"}
                    scored = [
                        (_score_page(claim_words, p, ch, pt), pid)
                        for pid, p, ch, pt in candidates
                    ]
                    scored.sort(key=lambda x: -x[0])
                    best_page_id = scored[0][1]

            # ── 전략 C: 기타 holder (skill_kb_bridge 등) ───────────────────
            else:
                claim_words = set(
                    _re.split(r'[\s\-_/:.]+', claim.lower())
                ) - {"", "the", "and", "or", "is", "a", "in", "of"}
                if len(claim_words) < 2:
                    continue  # 키워드 부족 → skip

                candidates = c.execute("""
                    SELECT id, path, char_count, page_type FROM pages
                    WHERE path != ''
                    ORDER BY char_count DESC
                    LIMIT 30
                """).fetchall()

                if candidates:
                    scored = [
                        (_score_page(claim_words, p, ch, pt), pid)
                        for pid, p, ch, pt in candidates
                        if _score_page(claim_words, p, ch, pt) > 0.1
                    ]
                    if scored:
                        scored.sort(key=lambda x: -x[0])
                        best_page_id = scored[0][1]

            if best_page_id:
                c.execute("UPDATE takes SET page_id=? WHERE id=?", (best_page_id, take_id))
                linked += 1

        db.commit()
        db.close()
        return linked
    except Exception as e:
        return 0


def react(brain_now, brain_prev, kanban_now, kanban_prev, state):
    # nova_kb_sync 실행 중 감지 (lock 확인)
    import fcntl as _fcntl
    _KB_SYNC_LOCK = "/tmp/nova_kb_sync.lock"
    try:
        _lfd = open(_KB_SYNC_LOCK, 'w')
        try:
            _fcntl.flock(_lfd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _fcntl.flock(_lfd, _fcntl.LOCK_UN)
        except OSError:
            _lfd.close()
            # kb_sync 실행 중 - heavy actions 스킵
            log("  [KB-SYNC] nova_kb_sync 실행 중 - heavy actions 스킵")
            return []  # react 조기 종료
        finally:
            try:
                _lfd.close()
            except Exception:
                pass
    except Exception:
        pass  # lock 파일 열기 실패 시 안전하게 진행

    acted = []
    R = REACT
    new_takes = brain_now["takes"] - brain_prev.get("takes", brain_now["takes"])

    # CRITICAL: health 하락
    if brain_now["health"] < R["health_critical"]:
        if can_act(state, "dream", R["dream_min_s"]):
            log(f"  CRITICAL health={brain_now['health']} → DreamCycle")
            push_event("HEALTH_CRITICAL", "CRITICAL", f"health={brain_now['health']}", "DreamCycle 즉시")
            if run("nova_dream_runner.sh", 620):
                state["last_dream"] = time.time()
                state["takes_at_last_dream"] = brain_now.get("takes", 0)
            acted.append("dream_critical")

    # CRITICAL: orphan 발생
    if brain_now["orphan"] >= R["orphan_max"]:
        if can_act(state, "fix_orphan", 30):
            log(f"  orphan={brain_now['orphan']} → 정리")
            act(state, "fix_orphan", fix_orphans)
            acted.append("fix_orphan")

    # HIGH: contradictions 발생 — 값이 증가했을 때만 새 이벤트 (중복 방지)
    # BUG-H4 fix: 하루 3회 cap 추가 (5분마다 최대 288개 방지)
    prev_contra = brain_prev.get("open_contra", 0)
    if brain_now["open_contra"] > 0 and can_act(state, "contra", 300):
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(DB) as _db:
            today_contra_cnt = _db.execute(
                "SELECT count(*) FROM hermes_events WHERE event_type='CONTRADICTION' AND created_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]
        if today_contra_cnt < 3:
            # 이전과 동일한 수라면 24시간 이내 중복 이벤트 방지
            if brain_now["open_contra"] != prev_contra or can_act(state, "contra", 86400):
                log(f"  contradictions {brain_now['open_contra']}개 → 헤르 알림")
                push_event("CONTRADICTION", "HIGH", f"모순 {brain_now['open_contra']}개", "검토 필요")
            else:
                log(f"  contradictions {brain_now['open_contra']}개 지속 중 (이벤트 중복 방지)")
        else:
            log(f"  contradictions {brain_now['open_contra']}개 — 오늘 {today_contra_cnt}회 이미 알림 (cap=3 도달)")
        state["last_contra"] = time.time()
        acted.append("contra_alert")

    # HIGH: kanban done 증가 → chain_engine
    if kanban_now and kanban_prev:
        new_done = kanban_now["done"] - kanban_prev.get("done", kanban_now["done"])
        if new_done > 0 and can_act(state, "chain", R["chain_min_s"]):
            log(f"  kanban done +{new_done} → chain_engine 즉시")
            if run("nova_chain_engine.py", 60): state["last_chain"] = time.time()
            acted.append("chain_engine")

    # takes 증가 시 page_id 자동 연결
    if new_takes > 0:
        linked = auto_link_takes_to_kb()
        if linked > 0:
            log(f"  takes {linked}개 → KB page_id 자동 연결")

    # MEDIUM: takes 증가
    if new_takes >= R["takes_for_dream"] and can_act(state, "dream", R["dream_min_s"]):
        log(f"  takes +{new_takes} → DreamCycle")
        if run("nova_dream_runner.sh", 620):  # 기존 400→620 (dream 최대 580s)
            state["last_dream"] = time.time()
            state["takes_at_last_dream"] = brain_now.get("takes", 0)
        acted.append("dream_takes")
    elif new_takes >= R["takes_for_synthesize"] and can_act(state, "synthesize", R["synthesize_min_s"]):
        log(f"  takes +{new_takes} → synthesize")
        if run("nova_brain_synthesize_runner.sh", 400): state["last_synthesize"] = time.time()
        acted.append("synthesize")
    elif new_takes >= R["takes_for_learn"] and can_act(state, "learn", R["learn_min_s"]):
        log(f"  takes +{new_takes} → learn_engine")
        if run("nova_learn_engine.py", 120): state["last_learn"] = time.time()
        acted.append("learn")

    # 외부 성장 신호 수집 — synthesize/dream/learn 반응 시 편승
    # cron 없이 시스템 활동 기반으로 RSS 체크 (6h 간격 제한)
    if any(a in acted for a in ["synthesize", "dream_takes", "dream_critical", "learn"]):
        if RESOURCE_UPDATER.exists() and can_act(state, "resource_update", RESOURCE_MIN_GAP_S):
            try:
                import subprocess as _sp
                _r = _sp.run(
                    ["python3", str(RESOURCE_UPDATER), "--domain", "all"],
                    capture_output=True, text=True, timeout=120
                )
                state["last_resource_update"] = time.time()
                out = (_r.stdout or "").strip().splitlines()
                tail = out[-1][:120] if out else "ok"
                log(f"  [RESOURCE] RSS 체크 완료: {tail}")
            except Exception as e:
                log(f"  [RESOURCE] RSS 체크 실패: {e}")

    # nova_wiki crosslink 편승 — synthesize 반응 시 (6h 간격)
    # wiki 페이지 간 상호 링크 정리 — synthesize가 새 pages 생성하므로 편승이 자연스럽다
    if any(a in acted for a in ["synthesize", "dream_takes", "dream_critical"]):
        if WIKI_SYNTH.exists() and can_act(state, "wiki_crosslink", WIKI_CROSSLINK_MIN_GAP_S):
            try:
                import subprocess as _sp
                _r = _sp.run(
                    ["python3", str(WIKI_SYNTH), "--phase", "crosslink"],
                    capture_output=True, text=True, timeout=300
                )
                state["last_wiki_crosslink"] = time.time()
                out = (_r.stdout or "").strip().splitlines()
                tail = out[-1][:120] if out else "ok"
                log(f"  [WIKI-CROSSLINK] crosslink 완료: {tail}")
            except Exception as e:
                log(f"  [WIKI-CROSSLINK] 실패: {e}")

    # nova_wiki takes phase 편승 — takes가 많이 쌓였을 때 (dream급 +100, 12h 간격)
    # takes summary → wiki/entities/nova-brain-takes-summary.md 갱신
    if any(a in acted for a in ["dream_takes", "dream_critical"]):
        if WIKI_SYNTH.exists() and can_act(state, "wiki_takes", WIKI_TAKES_MIN_GAP_S):
            try:
                import subprocess as _sp
                _r = _sp.run(
                    ["python3", str(WIKI_SYNTH), "--phase", "takes"],
                    capture_output=True, text=True, timeout=300
                )
                state["last_wiki_takes"] = time.time()
                out = (_r.stdout or "").strip().splitlines()
                tail = out[-1][:120] if out else "ok"
                log(f"  [WIKI-TAKES] takes summary 갱신: {tail}")
            except Exception as e:
                log(f"  [WIKI-TAKES] 실패: {e}")

    # nova_wiki stale 편승 — dream 반응 후 (1일 간격, 무거운 LLM 재생성)
    # 오래된 wiki 페이지 재생성 — dream 이후 KB/brain이 가장 최신 상태일 때 자연스럽다
    if any(a in acted for a in ["dream_takes", "dream_critical"]):
        if WIKI_SYNTH.exists() and can_act(state, "wiki_stale", WIKI_STALE_MIN_GAP_S):
            try:
                import subprocess as _sp
                _sp.Popen(
                    ["python3", str(WIKI_SYNTH), "--phase", "stale"],
                    stdout=open(f"{_HERMES_HOME}/logs/wiki_stale.log", "a"),
                    stderr=_sp.STDOUT
                )
                state["last_wiki_stale"] = time.time()
                log("  [WIKI-STALE] stale 재생성 시작 (백그라운드)")
            except Exception as e:
                log(f"  [WIKI-STALE] 실패: {e}")

    # nova_resource_collector — dream급 이벤트에서만 7일 간격으로 실행
    # nova-resource-seo/marketing/dev-weekly(PAUSED) 대체
    # 무겁기 때문에 Popen 백그라운드 + 7일 쿨다운으로 과부하 방지
    if any(a in acted for a in ["dream_takes", "dream_critical"]):
        if RESOURCE_COLLECTOR.exists() and can_act(state, "resource_collector", COLLECTOR_MIN_GAP_S):
            try:
                import subprocess as _sp
                _col_log = open(f"{_HERMES_HOME}/logs/resource_collector.log", "a")
                _sp.Popen(
                    ["python3", str(RESOURCE_COLLECTOR), "collect", "all"],
                    stdout=_col_log, stderr=_sp.STDOUT
                )
                state["last_resource_collector"] = time.time()
                log("  [COLLECTOR] nova_resource_collector 시작 (백그라운드, 7일 쿨다운)")
            except Exception as e:
                log(f"  [COLLECTOR] 실패: {e}")

    # MEMORY 사용률 체크 (30분에 한 번) → 85%+ 시 memory_slim 즉시 실행
    if can_act(state, "memory_check", 1800):
        mem = snap_memory()
        state["last_memory_check"] = time.time()
        state["memory_pct"] = mem["pct"]
        if mem["pct"] >= 85:
            log(f"  MEMORY {mem['pct']}% ≥ 85% → memory_slim 자동 실행")
            if run_memory_slim():
                state["last_memory_slim"] = time.time()
                new_mem = snap_memory()
                log(f"  MEMORY 슬림화 완료: {mem['pct']}% → {new_mem['pct']}%")
                push_event("MEMORY_SLIM", "INFO",
                    f"MEMORY 슬림화 완료 ({mem['pct']}% → {new_mem['pct']}%)",
                    "nova_brain_watcher 자동 트리거")
                # nova_brain.db takes에 히스토리 기록 (Codex 지적: missing HIGH)
                try:
                    _db = sqlite3.connect(DB, timeout=2)
                    _c = _db.cursor()
                    _now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    _tid = uuid.uuid4().hex[:16]
                    _c.execute(
                        "INSERT INTO takes (id,page_id,kind,holder,claim,weight,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (_tid, None, "fact", "nova-evaluator",
                         f"MEMORY 자동 슬림화: {mem['pct']}% → {new_mem['pct']}% (brain_watcher 트리거)",
                         0.88, _now, _now)
                    )
                    _db.commit()
                    _db.close()
                except Exception:
                    pass
                acted.append(f"memory_slim_{mem['pct']}pct")
            else:
                log(f"  MEMORY slim 실패 — nova_autonomous_engine에 위임")
                push_event("MEMORY_SLIM_FAIL", "HIGH",
                    f"MEMORY {mem['pct']}% — 자동 슬림화 실패",
                    "수동 점검 필요")

    return acted


def _bootstrap_once(brain_now, kanban_now, state):
    """재시작 직후에도 누적 상태를 한 번 평가."""
    prev = {
        "takes": state.get("takes_at_last_dream", brain_now.get("takes", 0)),
        "open_contra": 0,
        "orphan": 0,
        "health": 100.0,
    }
    acted = react(brain_now, prev, kanban_now, kanban_now, state)
    # 부트스트랩 시 미읽 GEO 이벤트 처리
    _check_blog_geo_events(state)
    if can_act(state, "stagnant_check", 3600):
        stagnant_list = detect_stagnant_agents()
        state["last_stagnant_check"] = time.time()
        if stagnant_list:
            prev_stagnant = set(state.get("stagnant_agents_prev", []))
            new_stagnant = {s["agent"] for s in stagnant_list}
            newly_stagnant = new_stagnant - prev_stagnant
            state["stagnant_agents_prev"] = list(new_stagnant)
            if newly_stagnant:
                detail = "; ".join(
                    f"{s['agent']}(hq={s['hq_ratio']:.0f}%,n={s['sample_n']})"
                    for s in stagnant_list if s["agent"] in newly_stagnant
                )
                log(f"  [STAGNANT] 신규 감지 {len(newly_stagnant)}개: {detail}")
                push_event("STAGNANT_AGENT", "HIGH",
                           f"STAGNANT 에이전트 {len(newly_stagnant)}개 신규 감지",
                           detail[:300])
    return acted


def _watch_target_dirs() -> list[str]:
    """DB 파일이 실제로 위치한 경로만 감시 — /root/.hermes 전체 금지(노이즈 발생).
    nova_brain.db → /root/.hermes 최상위 (non-recursive)
    kanban.db → 각 보드 디렉토리만 (non-recursive)
    """
    targets = [_HERMES_HOME]   # nova_brain.db 위치 (최상위만, -r 없이도 동작)
    boards_root = Path(_HERMES_HOME) / "kanban/boards"
    if boards_root.exists():
        for board_dir in boards_root.iterdir():
            if board_dir.is_dir() and (board_dir / "kanban.db").exists():
                targets.append(str(board_dir))
    return targets


# ISDIR 재시작이 허용되는 경로 접두사 — DB 경로 상위가 아닌 곳은 무시
_WATCH_DIR_PREFIXES_ALLOWED_RESTART = [
    f"{_HERMES_HOME}/kanban/boards",
]


def _spawn_db_inotify():
    return subprocess.Popen(
        [
            "inotifywait",
            "-m",
            # -r (recursive) 제거 — /root/.hermes 하위 전체를 감시하면
            # skills/.curator_backups/ 등 무관 경로 이벤트가 다량 발생 (노이즈)
            # _watch_target_dirs()가 정확한 경로만 반환하므로 recursive 불필요
            "-e", "close_write,create,moved_to,delete",
            "--format", "%w|%f|%e",
            # BUG-INOTIFY-FLOOD 수정 (2026-07-22):
            # /root/.hermes/ 전체 감시 시 초당 6600+ WAL/SHM 이벤트 → Python 100% CPU 스핀
            # --include $ 앵커로 .db-wal/.db-shm 제외, 메인 DB 파일만 필터링
            "--include", r"(nova_brain|kanban)\.db$",
            *_watch_target_dirs(),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _relevant_db_event(path: Path) -> bool:
    name = path.name
    # BUG-WAL-SPIN 수정 (OSS 동기화 2026-07-21):
    # WAL/SHM 파일은 read-only 연결 시에도 빈번하게 이벤트 발생 → CPU 99% 스핀
    # 메인 DB 파일만 감시하면 실제 변경을 충분히 감지 가능
    return name in {
        "nova_brain.db",
        "kanban.db",
    }


def _run_blog_geo_engine(state, event_type="daily", blog=None, url="", title=""):
    """
    blog_geo_engine.py 실행.
    event_type='publish': 발행 직후 훅 (5분 쿨다운)
    event_type='daily':   일일 전체 분석 (20h 쿨다운)
    """
    if not GEO_ENGINE.exists():
        return

    if event_type == "publish":
        if not can_act(state, "geo_engine_publish", GEO_ENGINE_MIN_S):
            return
        cmd = [
            "python3", str(GEO_ENGINE),
            "--event", "publish",
            "--blog", blog or "",
            "--url",  url,
            "--title", title,
        ]
        log(f"  [GEO-HOOK] BLOG_PUBLISHED → geo_engine publish ({blog})")
        state["last_geo_engine_publish"] = time.time()
    else:
        if not can_act(state, "geo_engine_daily", GEO_DAILY_MIN_S):
            return
        cmd = ["python3", str(GEO_ENGINE)]
        log(f"  [GEO-DAILY] geo_engine 일일 분석 실행")
        state["last_geo_engine_daily"] = time.time()

    try:
        import subprocess as _sp
        _geo_log = open(f"{_HERMES_HOME}/logs/blog_geo_engine.log", "a")
        _sp.Popen(cmd, stdout=_geo_log, stderr=_sp.STDOUT)
    except Exception as e:
        log(f"  [GEO-ENGINE-ERR] {e}")


def _check_blog_geo_events(state):
    """
    hermes_events 에서 미읽 BLOG_PUBLISHED / BLOG_GEO_DAILY 이벤트 감지.
    watcher가 DB 변화를 감지한 직후 호출.
    """
    if not Path(DB).exists():
        return
    try:
        import re as _re2
        db = sqlite3.connect(DB, timeout=2)
        # 미읽 BLOG_PUBLISHED 이벤트 (최근 10분 이내)
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=10)).isoformat()
        published = db.execute(
            "SELECT id, title, detail FROM hermes_events "
            "WHERE event_type='BLOG_PUBLISHED' AND is_read=0 AND created_at >= ? "
            "ORDER BY created_at ASC LIMIT 5",
            (cutoff,)
        ).fetchall()
        for eid, title, detail in published:
            # detail = "url=https://..."
            url   = ""
            blog  = ""
            if detail:
                m = _re2.search(r"url=(\S+)", detail)
                if m:
                    url = m.group(1)
            m2 = _re2.search(r"\[(\w+)\]", title or "")
            if m2:
                blog = m2.group(1)
            db.execute("UPDATE hermes_events SET is_read=1 WHERE id=?", (eid,))
            db.commit()
            _run_blog_geo_engine(state, "publish", blog=blog, url=url, title=title or "")

        # 일일 분석 이벤트 (BLOG_GEO_DAILY 트리거)
        daily_evt = db.execute(
            "SELECT id FROM hermes_events "
            "WHERE event_type='BLOG_GEO_DAILY_TRIGGER' AND is_read=0 "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if daily_evt:
            db.execute("UPDATE hermes_events SET is_read=1 WHERE id=?", (daily_evt[0],))
            db.commit()
            _run_blog_geo_engine(state, "daily")

        db.close()
    except Exception as e:
        log(f"  [GEO-CHECK-ERR] {e}")


def _run_stagnant_check(state):
    if not can_act(state, "stagnant_check", 3600):
        return
    stagnant_list = detect_stagnant_agents()
    state["last_stagnant_check"] = time.time()
    if stagnant_list:
        prev_stagnant = set(state.get("stagnant_agents_prev", []))
        new_stagnant = {s["agent"] for s in stagnant_list}
        newly_stagnant = new_stagnant - prev_stagnant
        state["stagnant_agents_prev"] = list(new_stagnant)
        if newly_stagnant:
            detail = "; ".join(
                f"{s['agent']}(hq={s['hq_ratio']:.0f}%,n={s['sample_n']})"
                for s in stagnant_list if s["agent"] in newly_stagnant
            )
            log(f"  [STAGNANT] 신규 감지 {len(newly_stagnant)}개: {detail}")
            push_event("STAGNANT_AGENT", "HIGH",
                       f"STAGNANT 에이전트 {len(newly_stagnant)}개 신규 감지",
                       detail[:300])

            # STAGNANT 발생 시 audit_loop 편승 실행 (12h 간격 제한)
            if AUDIT_LOOP_SH.exists() and can_act(state, "audit_loop", AUDIT_MIN_GAP_S):
                try:
                    import subprocess as _subp
                    _audit_log = open(f"{_HERMES_HOME}/logs/audit_loop.log", "a")
                    _subp.Popen(["bash", str(AUDIT_LOOP_SH)],
                                stdout=_audit_log,
                                stderr=_subp.STDOUT)
                    state["last_audit_loop"] = time.time()
                    log(f"  [AUDIT] STAGNANT 감지 → audit_loop 실행")
                except Exception as e:
                    log(f"  [AUDIT] audit_loop 실행 실패: {e}")


def main():
    log("시작 — inotify event-driven, DB/kanban 변화 기반 판단")
    state = load_state()

    brain_prev = snap_brain() or {}
    kanban_prev = snap_kanban() or {}

    if "takes_at_last_dream" not in state:
        state["takes_at_last_dream"] = brain_prev.get("takes", 0)

    boot_acted = _bootstrap_once(brain_prev, kanban_prev, state)
    if boot_acted:
        log(f"bootstrap 반응: {boot_acted}")
        save_state(state)

    # BUG-INOTIFY-FLOOD 수정 (2026-07-22):
    # kanban.db 초당 800+회 쓰기 + WAL/SHM 이벤트 → Python 100% CPU 스핀
    # 최소 처리 간격(REACT_MIN_INTERVAL) 적용: 이벤트는 드레인하되 snap/react는 throttle
    REACT_MIN_INTERVAL = 3.0   # 초당 최대 0.33회 DB 스냅샷+반응
    _last_react_ts: float = 0.0

    while True:
        proc = _spawn_db_inotify()
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "|" not in line:
                    continue
                watch_dir, filename, events = line.split("|", 2)
                full = Path(watch_dir) / filename

                if "ISDIR" in events and ("CREATE" in events or "MOVED_TO" in events):
                    # 허용된 경로(kanban/boards)에서 새 보드 디렉토리 생성 시만 재시작
                    # skills/.curator_backups/ 등 무관 경로 이벤트는 무시
                    full_str = str(full)
                    if any(full_str.startswith(p) for p in _WATCH_DIR_PREFIXES_ALLOWED_RESTART):
                        log(f"새 보드 디렉토리 감지 → watcher 재시작: {full}")
                        break
                    else:
                        # 무관 디렉토리 이벤트 — 무시 (노이즈 방지)
                        continue

                if not _relevant_db_event(full):
                    continue

                # 스로틀: 최소 REACT_MIN_INTERVAL 초 경과 후에만 snap/react 실행
                # pipe는 계속 드레인(위 continue들)하되 처리는 제한 — CPU 과점 방지
                now_ts = time.time()
                if now_ts - _last_react_ts < REACT_MIN_INTERVAL:
                    continue
                _last_react_ts = now_ts

                brain_now = snap_brain()
                kanban_now = snap_kanban()
                if brain_now is None:
                    continue

                brain_changed = (brain_now != brain_prev)
                kanban_changed = kanban_now and (kanban_now != kanban_prev)
                if not (brain_changed or kanban_changed):
                    continue

                acted = react(brain_now, brain_prev, kanban_now, kanban_prev, state)
                if brain_changed and (brain_now.get("takes", 0) > brain_prev.get("takes", 0)):
                    _run_stagnant_check(state)

                # GEO 이벤트 감지 (DB 변화 시마다 미읽 이벤트 체크)
                if brain_changed:
                    _check_blog_geo_events(state)

                if acted:
                    log(f"event 반응: {acted}")
                    save_state(state)

                brain_prev = brain_now
                if kanban_now:
                    kanban_prev = kanban_now
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            save_state(state)
            time.sleep(1)


if __name__ == "__main__":
    main()
