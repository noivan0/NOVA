#!/usr/bin/env python3
"""nova_brain.db 상태 조회 - nova_audit_loop용 (헤르/헤르2 공용)"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import sqlite3, sys, os

DB_PATH = os.environ.get("NOVA_BRAIN_DB", f"{_HERMES_HOME}/nova_brain.db")

try:
    db = sqlite3.connect(DB_PATH)
    c = db.cursor()
    takes = c.execute("SELECT count(*) FROM takes").fetchone()[0]
    # 실시간 쿼리 (brain_health snapshot 아님)
    open_c = c.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
    dismissed = c.execute("SELECT count(*) FROM contradictions WHERE status='dismissed'").fetchone()[0]
    resolved = c.execute("SELECT count(*) FROM contradictions WHERE status='resolved'").fetchone()[0]
    # orphan = agent IS NULL AND page_type='general'
    orphan = c.execute("SELECT COUNT(*) FROM pages WHERE agent IS NULL AND page_type='general'").fetchone()[0]
    self_scores = c.execute("SELECT count(*) FROM agent_activity").fetchone()[0]
    # score는 brain_health 최신 — score_coverage/total_pages/pages_with_takes 포함
    bh = c.execute(
        "SELECT score_overall, score_coverage, total_pages, pages_with_takes "
        "FROM brain_health ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    db_size = os.path.getsize(DB_PATH) / 1024
    db.close()

    if bh:
        score_overall, score_cov, total_pages, pwt = bh
        real_pct = (pwt / max(total_pages, 1)) * 100
        # 병기: 내부×200 점수 / 실제 커버리지 %
        coverage_str = f"{score_cov:.1f}(내부×200) / 실커버리지={real_pct:.1f}%({pwt}/{total_pages})"
        score = score_overall
    else:
        score = "N/A"
        coverage_str = "N/A"

    print(f"✅ nova_brain.db: {db_size:.0f}KB")
    print(f"   takes={takes} / dismissed_contradictions={dismissed} / open_contradictions={open_c} / resolved={resolved} / orphan_pages={orphan} / self_scores={self_scores}")
    print(f"   health_score={score} / score_coverage={coverage_str}")
except Exception as e:
    print(f"❌ DB 오류: {e}", file=sys.stderr)
    sys.exit(1)
