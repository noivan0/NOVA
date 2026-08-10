#!/usr/bin/env python3
"""
nova_kb_sync.py — kb_sync.py 대체 (nova_brain.db 단일화)

기존 kb_sync.py 역할:
  - KB 파일 변경 감지 → embeddings.db 인덱싱 + 벡터화

이 파일:
  - KB 파일 변경 감지 → nova_brain.db 인덱싱 + 벡터화
  - embeddings.db는 레거시 유지 (호환성)
  - nova_brain.db가 primary 검색 소스

실행:
  python3 nova_kb_sync.py               # 변경된 파일만 sync
  python3 nova_kb_sync.py --reindex-all # 전체 재인덱싱
  python3 nova_kb_sync.py --stats       # 통계
"""
import sys
import os
import hashlib
import argparse
from pathlib import Path

HERMES_HOME    = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
KB_ROOT        = HERMES_HOME / "kb"          # 기본 KB (호환성 유지)
NOVA_BRAIN_PY  = HERMES_HOME / "bin" / "nova_brain.py"
# 메타/구조 파일 제외 — KB 콘텐츠 아닌 파일들
# (index, log, schema, template 등은 nova_brain DB에 인덱싱 불필요)
# 참고: Round N+5 교차검증 V1에서 wiki 814개 vs DB 799개(15개 누락) 실측 결과,
#       누락된 15개가 모두 이 제외 목록에 해당하는 메타/인덱스 파일임이 확인됨(의도적 제외)
SKIP_FILES     = {"index.md","log.md","log-2026.md","SCHEMA.md",
                  "_registry.md","TEMPLATE.md","memory_pending.md","DESCRIPTION.md"}

# .confluence.md 확장자: Confluence 연동 전용 미러 파일
# — nova_brain DB 인덱싱 대상 아님 (원본 KB 파일이 별도 존재, 중복 방지)
SKIP_SUFFIXES  = {".confluence.md", ".md.md"}  # .md.md: 이중확장자 dead link 방지 (2026-07-30)

# archive/weekly는 별도 정책으로 제외.
# kb/skills/ 는 skill_kb_bridge가 생성하는 KB 요약 페이지이므로 인덱싱 대상이다.
SKIP_DIRS      = {"archive","weekly","__pycache__"}

# 다중 스캔 경로 (prefix → page_type 매핑)
NOVA_HOME_PATH = Path(os.environ.get("NOVA_HOME", str(Path.home() / ".nova")))
SCAN_ROOTS = [
    (KB_ROOT,                          "kb/",         None),
    (NOVA_HOME_PATH / "wiki",          "wiki/",       "wiki"),      # BUG-W5 수정: concept→wiki
    (HERMES_HOME / "memories",         "memories/",   "memory"),    # BUG-W3: memories 추가
    (HERMES_HOME / "doosi" / "kb",     "doosi/",      "project"),
    (NOVA_HOME_PATH / "workspace",     "workspace/",  "harness"),  # harness 결과물
]

WORKSPACE_INCLUDE = {"report.md", "summary_report.md", "insights.md", "synthesis.md"}

def _should_include(root: Path, file_path: Path) -> bool:
    nova_ws = NOVA_HOME_PATH / "workspace"
    try:
        file_path.relative_to(nova_ws)
        return file_path.name in WORKSPACE_INCLUDE
    except ValueError:
        return True

# profiles/nova-*/evolution.md 만 선택 추가 (스킬 복사본 제외)
AGENT_PROFILES = [
    (HERMES_HOME / "profiles" / ag / "evolution.md", f"profiles/{ag}/evolution.md", "agent")
    for ag in [
        "nova-autoplan","nova-benchmark","nova-canary","nova-careful","nova-checkpoint",
        "nova-cso","nova-dev","nova-document","nova-document-release","nova-evaluator",
        "nova-health","nova-investigate","nova-learn","nova-marketing","nova-qa",
        "nova-research","nova-retro","nova-review","nova-ship","nova-strategy","nova-validator"
    ]
    if (HERMES_HOME / "profiles" / ag / "evolution.md").exists()
]


def get_brain():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nova_brain", str(NOVA_BRAIN_PY))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.NovaBrain()


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def sync_changed(embed: bool = True) -> dict:
    # flock으로 이중 실행 방지 (Codex MEDIUM: kb_watcher + brain_watcher 동시 호출 race condition)
    import fcntl
    SYNC_LOCK = "/tmp/nova_kb_sync.lock"
    lock_fd = open(SYNC_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0, "locked": True}

    try:
        return _sync_changed_inner(embed=embed)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _sync_changed_inner(embed: bool = True) -> dict:
    brain = get_brain()
    result = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0}

    # 다중 경로 스캔
    scan_targets = []
    for scan_root, prefix, agent_hint in SCAN_ROOTS:
        if scan_root.exists():
            for f in scan_root.rglob("*.md"):
                if f.name in SKIP_FILES:
                    continue
                if any(f.name.endswith(sfx) for sfx in SKIP_SUFFIXES):
                    continue
                if any(d in str(f) for d in SKIP_DIRS):
                    continue
                if not _should_include(scan_root, f):
                    continue
                rel = prefix + str(f.relative_to(scan_root))
                scan_targets.append((f, rel, agent_hint))

    # 에이전트 evolution.md 추가
    for f_path, rel, agent_hint in AGENT_PROFILES:
        p = Path(f_path)
        if p.exists():
            scan_targets.append((p, rel, agent_hint))

    for f, rel, agent_hint in scan_targets:
        result["scanned"] += 1
        try:
            # Codex MEDIUM BUG fix: file_hash(raw bytes) vs pages.content_hash(parsed content) 불일치
            # nova_brain.py의 _page_id 방식과 동일하게 rel(경로) sha256 기반으로 page_id 생성 후
            # content_hash는 nova_brain이 계산한 값을 그대로 사용 (재계산 불필요)
            # → 변경 감지는 파일 mtime 기반으로 전환 (hash 불일치 문제 근본 해결)
            import os
            page_id = brain._page_id(rel)
            row = brain.conn.execute(
                "SELECT content_hash, indexed_at FROM pages WHERE id=?", (page_id,)
            ).fetchone()

            # mtime 기반 변경 감지 (hash 불일치 회피)
            try:
                file_mtime = str(int(f.stat().st_mtime))
            except Exception:
                file_mtime = ""

            # indexed_at이 파일 mtime보다 최신이면 스킵 (변경 없음)
            # 단, 청크가 없는 경우에는 강제 재인덱싱 (chunk fix 이전 인덱싱된 페이지 대응)
            if row and row[1]:  # indexed_at 존재
                try:
                    import datetime as _dt
                    indexed_ts = _dt.datetime.fromisoformat(row[1].replace("Z", "+00:00")).timestamp()
                    if indexed_ts >= f.stat().st_mtime:
                        # mtime OK지만 청크 존재 여부도 확인 (chunk fix 전 인덱싱 파일 재처리)
                        has_chunks = brain.conn.execute(
                            "SELECT 1 FROM page_chunks WHERE page_id=? LIMIT 1", (page_id,)
                        ).fetchone()
                        if has_chunks:
                            result["skipped"] += 1
                            continue
                        # 청크 없음 → 강제 재인덱싱 (chunk fix 이전에 indexed_at만 기록된 케이스)
                except Exception:
                    pass  # 파싱 실패 시 재인덱싱

            # abs_path를 직접 전달 (다중 경로 지원)
            ok = brain.index_kb_file_abs(str(f), rel, embed=embed, agent_hint=agent_hint)
            if ok:
                if row:
                    result["updated"] += 1
                else:
                    result["added"] += 1
            else:
                result["skipped"] += 1
            
            # 100개마다 중간 commit (DB write lock 보유 시간 단축)
            total_processed = result["added"] + result["updated"]
            if total_processed % 100 == 0 and total_processed > 0:
                try:
                    brain.conn.commit()
                    brain.conn.execute('PRAGMA wal_checkpoint(PASSIVE)')  # WAL checkpoint 추가
                except Exception:
                    pass
        except Exception as e:
            result["errors"] += 1
            print(f"[ERROR] {rel}: {e}", file=sys.stderr)

    brain.conn.commit()
    brain.close()
    return result


def _resolve_single_target(path_str: str):
    p = Path(path_str).resolve()

    for scan_root, prefix, agent_hint in SCAN_ROOTS:
        try:
            rel = prefix + str(p.relative_to(scan_root))
        except ValueError:
            continue
        if p.name in SKIP_FILES:
            return None
        if any(p.name.endswith(sfx) for sfx in SKIP_SUFFIXES):
            return None
        if any(part in SKIP_DIRS for part in p.parts):
            return None
        return p, rel, agent_hint

    for f_path, rel, agent_hint in AGENT_PROFILES:
        if p == Path(f_path).resolve():
            return p, rel, agent_hint

    return None


def sync_one(path_str: str, embed: bool = True) -> dict:
    import fcntl
    SYNC_LOCK = "/tmp/nova_kb_sync.lock"
    lock_fd = open(SYNC_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0, "locked": True}

    try:
        target = _resolve_single_target(path_str)
        result = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0}
        if not target:
            result["errors"] = 1
            return result

        f, rel, agent_hint = target
        brain = get_brain()
        result["scanned"] = 1
        try:
            page_id = brain._page_id(rel)
            row = brain.conn.execute(
                "SELECT content_hash, indexed_at FROM pages WHERE id=?", (page_id,)
            ).fetchone()
            ok = brain.index_kb_file_abs(str(f), rel, embed=embed, agent_hint=agent_hint)
            if ok:
                if row:
                    result["updated"] += 1
                else:
                    result["added"] += 1
            else:
                result["skipped"] += 1
            brain.conn.commit()
        except Exception as e:
            result["errors"] += 1
            print(f"[ERROR] {rel}: {e}", file=sys.stderr)
        finally:
            brain.close()
        return result
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def show_stats():
    brain = get_brain()
    s = brain.stats()
    print("nova_brain.db 통계:")
    for k, v in s.items():
        print(f"  {k}: {v}")
    brain.close()


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()

    parser = argparse.ArgumentParser()
    parser.add_argument("--reindex-all", action="store_true")
    parser.add_argument("--stats",       action="store_true")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--file",        help="단일 파일만 sync")
    parser.add_argument("--no-embed",    action="store_true",
                        help="벡터 없이 메타/청크만 (빠름)")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        sys.exit(0)

    if args.reindex_all:
        import subprocess
        r = subprocess.run(
            [sys.executable, str(NOVA_BRAIN_PY), "index-all"],
            capture_output=True, text=True
        )
        print(r.stdout)
        sys.exit(0)

    # 기본: 변경된 파일만 sync
    embed = not args.no_embed
    result = sync_one(args.file, embed=embed) if args.file else sync_changed(embed=embed)
    total = result["added"] + result["updated"]
    print(f"[nova_kb_sync] 스캔={result['scanned']} | "
          f"추가={result['added']} | 업데이트={result['updated']} | "
          f"스킵={result['skipped']} | 오류={result['errors']}")
    # kb_sync.py 호환 형식 출력 (kb_pipeline.py가 파싱)
    print(f"[완료] 스캔={result['scanned']}개 | "
          f"추가={result['added']} | 업데이트={result['updated']} | "
          f"임베딩={total} | 스킵={result['skipped']} | 오류={result['errors']}")

    # KB 동기화 후 → wiki 자동 갱신 (신규/갱신 파일이 있을 때만)
    if total > 0:
        kb_wiki_bridge = HERMES_HOME / "bin" / "nova_kb_wiki_bridge.py"
        if kb_wiki_bridge.exists():
            try:
                import subprocess as _sp
                r = _sp.run(
                    [sys.executable, str(kb_wiki_bridge), "--sync"],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, "HERMES_HOME": str(HERMES_HOME),
                         "NOVA_HOME": str(NOVA_HOME)}
                )
                if r.returncode == 0:
                    print(f"[nova_kb_sync] wiki 자동 갱신 완료")
                else:
                    print(f"[nova_kb_sync] wiki 갱신 실패: {r.stderr[:100]}")
            except Exception as e:
                print(f"[nova_kb_sync] wiki 갱신 예외: {e}")
