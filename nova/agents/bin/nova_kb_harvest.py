#!/usr/bin/env python3
"""
nova_kb_harvest.py — harness workspace report.md → KB projects/nova-harness-log.md 자동 기록
brain_watcher harness 실행 후 호출. nova_kb_full_sync.sh 에서도 실행.

흐름:
  $NOVA_HOME/workspace/{harness}/report.md  →  brain.db pages 인덱싱
  최신 20개 harness report 요약  →  KB/projects/nova-harness-log.md 갱신
"""
import sqlite3, os, hashlib
from pathlib import Path
from datetime import datetime, timezone

NOVA_HOME   = Path(os.environ.get("NOVA_HOME",   str(Path.home()/".nova")))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes")))
BRAIN_DB    = NOVA_HOME / "brain.db"
KB          = HERMES_HOME / "kb"
WORKSPACE   = NOVA_HOME / "workspace"
LOG_KEY     = "projects/nova-harness-log.md"

HARNESS_NAMES = [
    "research", "investigate", "code_implement", "code_review",
    "document_gen", "document_release", "learn", "retro",
    "go_nogo", "kpi_evaluate", "system_audit",
]

now = datetime.now(timezone.utc)
now_str = now.strftime("%Y-%m-%d %H:%M UTC")

# 1. workspace report.md → brain.db 인덱싱
indexed = 0
with sqlite3.connect(str(BRAIN_DB), timeout=5) as db:
    db.execute("PRAGMA journal_mode=WAL")
    for h in HARNESS_NAMES:
        report = WORKSPACE / h / "report.md"
        if not report.exists():
            continue
        try:
            content = report.read_text(errors="ignore")
            path_key = f"nova_workspace/{h}/report.md"
            chash = hashlib.sha256(content.encode()).hexdigest()[:16]
            exists = db.execute(
                "SELECT id, content_hash FROM pages WHERE path=?", (path_key,)
            ).fetchone()
            if exists:
                if exists[1] != chash:
                    db.execute(
                        "UPDATE pages SET compiled_truth=?, content_hash=?, char_count=?, updated_at=? WHERE path=?",
                        (content[:3000], chash, len(content), now.isoformat(), path_key)
                    )
                    indexed += 1
            else:
                import hashlib as _hl
                page_id = _hl.sha256(path_key.encode()).hexdigest()[:16]
                db.execute(
                    "INSERT INTO pages (id, path, title, page_type, agent, compiled_truth, content_hash, char_count, indexed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (page_id, path_key, f"harness:{h} report", "harness", "harness",
                     content[:3000], chash, len(content), now.isoformat())
                )
                indexed += 1
        except Exception as e:
            print(f"  [{h}] ERROR: {e}")

print(f"[harvest] {indexed}개 harness report 갱신 → brain.db")

# 2. KB/projects/nova-harness-log.md 갱신
lines = [
    f"# NOVA Harness 실행 로그\n",
    f"최종갱신: {now_str}\n",
    "---\n",
    "## 최신 harness workspace 보고서\n",
]

for h in HARNESS_NAMES:
    report = WORKSPACE / h / "report.md"
    if not report.exists():
        continue
    mtime = datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc)
    mtime_str = mtime.strftime("%Y-%m-%d %H:%M UTC")
    size = report.stat().st_size
    # 요약: 첫 5줄
    try:
        preview = "\n".join(report.read_text(errors="ignore").split("\n")[:5])
        preview = preview.replace("#", "").strip()[:200]
    except Exception:
        preview = "(읽기 실패)"
    lines.append(f"\n### {h}\n")
    lines.append(f"- 최종실행: {mtime_str} | 크기: {size:,}B\n")
    lines.append(f"- 내용 요약: {preview}\n")

lines.append("\n---\n")

out_path = KB / LOG_KEY
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text("".join(lines), encoding="utf-8")
print(f"[harvest] {LOG_KEY} 갱신 완료")

# 3. log.md append
log_path = KB / "log.md"
with open(str(log_path), "a", encoding="utf-8") as f:
    f.write(f"\n## [{now_str}] harvest | harness workspace → KB 동기화 ({indexed}개 갱신)")

print("[harvest] 완료")
