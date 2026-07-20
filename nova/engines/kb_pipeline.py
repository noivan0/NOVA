"""kb_pipeline.py — KB MD → brain.db pages 동기화

수정 (2026-07-19): id 자동 생성(sha256) + ON CONFLICT upsert — NULL id 재발 영구 차단
"""
import sys, sqlite3, hashlib
from pathlib import Path

BRAIN = Path("/home/user/.nova/brain.db")
KB    = Path("/mnt/d/hermes/.hermes/kb")


def _page_id(path_str: str) -> str:
    return hashlib.sha256(path_str.encode()).hexdigest()[:16]


def _upsert(c: sqlite3.Connection, rel_path: str, title: str, content: str, size: int) -> None:
    pid = _page_id(rel_path)
    c.execute(
        """INSERT INTO pages (id, path, title, compiled_truth, char_count)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title          = excluded.title,
             compiled_truth = excluded.compiled_truth,
             char_count     = excluded.char_count,
             updated_at     = datetime('now')
        """,
        (pid, rel_path, title, content[:2000], size),
    )


def run(changed_path: str | None = None) -> None:
    with sqlite3.connect(str(BRAIN)) as c:
        schema = [d[1] for d in c.execute("PRAGMA table_info(pages)").fetchall()]
        if "path" not in schema or "title" not in schema:
            print("schema 불일치 — skip")
            return

        if changed_path:
            p = Path(changed_path)
            if p.exists() and p.suffix == ".md":
                content = p.read_text(errors="ignore")
                rel     = str(p.relative_to(KB))
                _upsert(c, rel, p.stem, content, len(content))
                print(f"indexed: {p.name}")
        else:
            cnt = 0
            for md in sorted(KB.rglob("*.md")):
                if md.name in ("log.md", "TEMPLATE.md"):
                    continue
                ct  = md.read_text(errors="ignore")
                rel = str(md.relative_to(KB))
                _upsert(c, rel, md.stem, ct, len(ct))
                cnt += 1
            print(f"full KB scan: {cnt} files upserted into brain.db pages")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
