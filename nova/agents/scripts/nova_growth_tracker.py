import os
#!/usr/bin/env python3
"""nova_brain.db + embeddings.db 성장속도 측정 — A안 구현"""
import os as _os
from pathlib import Path as _Path
_HERMES_HOME = _os.environ.get("HERMES_HOME", str(_Path.home() / ".hermes"))

import sqlite3, os
from datetime import datetime, timezone, timedelta

BRAIN_DB = f"{_HERMES_HOME}/nova_brain.db"
EMBED_DB = f"{_HERMES_HOME}/embeddings.db"

def measure():
    results = {}
    
    # nova_brain.db
    db = sqlite3.connect(BRAIN_DB)
    c = db.cursor()
    
    takes_total = c.execute("SELECT count(*) FROM takes").fetchone()[0]
    takes_7d = c.execute("""
        SELECT count(*) FROM takes 
        WHERE created_at >= datetime('now','-7 days')
    """).fetchone()[0]
    takes_today = c.execute("""
        SELECT count(*) FROM takes 
        WHERE date(created_at) = date('now')
    """).fetchone()[0]
    
    pages_total = c.execute("SELECT count(*) FROM pages").fetchone()[0]
    open_c = c.execute("SELECT count(*) FROM contradictions WHERE status='open'").fetchone()[0]
    score = c.execute("SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1").fetchone()
    db.close()
    
    # embeddings.db
    edb = sqlite3.connect(EMBED_DB)
    ec = edb.cursor()
    skill_cnt = ec.execute("SELECT count(*) FROM skill_embeddings").fetchone()[0]
    kb_cnt = ec.execute("SELECT count(*) FROM kb_embeddings").fetchone()[0]
    kb_today = ec.execute("SELECT count(*) FROM kb_embeddings WHERE date(indexed_at) = date('now')").fetchone()[0]
    edb.close()
    
    print(f"📊 nova_brain 성장속도 ({datetime.now().strftime('%Y-%m-%d %H:%M')} KST)")
    print(f"  takes: {takes_total}건 (오늘+{takes_today} / 7일+{takes_7d})")
    print(f"  pages: {pages_total}건")
    print(f"  open_contradictions: {open_c}")
    print(f"  health_score: {score[0] if score else 'N/A'}")
    print(f"  L8 skill_embeddings: {skill_cnt}개")
    print(f"  kb_embeddings: {kb_cnt}건 (오늘+{kb_today})")
    print(f"  DB크기: {os.path.getsize(BRAIN_DB)/1024/1024:.1f}MB")

    # C) on_fail 통계 (chain_engine 로그)
    import re
    from pathlib import Path
    chain_log_dir = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "cron/output/b377b67a32aa"
    if chain_log_dir.exists():
        logs = sorted(chain_log_dir.glob("*.md"), reverse=True)[:10]
        skip_fail = sum(len(re.findall(r"\[SKIP-FAIL\]", l.read_text(errors="ignore"))) for l in logs)
        dod_fail = sum(len(re.findall(r"\[DoD FAIL\]", l.read_text(errors="ignore"))) for l in logs)
        print(f"  on_fail SKIP-FAIL: {skip_fail}건 / DoD FAIL: {dod_fail}건 (최근 {len(logs)}회)")
    else:
        print("  on_fail: chain_engine 로그 없음")


if __name__ == '__main__':
    measure()
