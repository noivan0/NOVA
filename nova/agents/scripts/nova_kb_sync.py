#!/usr/bin/env python3
"""
nova_kb_sync.py — evolution.md + metrics.json → KB 자동 동기화
매 15분 크론으로 실행하여 3개 프로젝트 성장 데이터를 KB에 반영
"""
import fcntl
import json
import os
import pathlib
import tempfile
from datetime import datetime

# [R16-CX-002-FIX] /root 하드코딩 제거 → HERMES_HOME 환경변수 + Path.home() 폴백
import os
HERMES_DIR = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / ".hermes")))
PROJECTS_DIR = HERMES_DIR / 'projects'
KB_DIR = HERMES_DIR / 'kb' / 'projects'
KB_LOG = HERMES_DIR / 'kb' / 'log.md'

# [M-3 fix] 고정 목록 대신 동적 로드 — _projects_overrides.json + 기본 목록 병합
_BASE_PROJECTS = ['blog-pipeline', 'shortform-video', 'unlearning']

def _load_all_projects() -> list:
    """기본 프로젝트 + _projects_overrides.json 동적 프로젝트 목록 반환"""
    projects = list(_BASE_PROJECTS)
    overrides_file = PROJECTS_DIR / '_projects_overrides.json'
    if overrides_file.exists():
        try:
            ov = json.loads(overrides_file.read_text(encoding='utf-8'))
            for name in ov:
                if name not in projects:
                    projects.append(name)
        except Exception as e:
            print(f"[nova_kb_sync] _projects_overrides.json 파싱 실패 (무시): {e}")  # [B-15 FIX] silent → 로그
    return projects

def sync_project(name: str) -> str:
    proj_dir = PROJECTS_DIR / name
    evo_file = proj_dir / 'evolution.md'
    metrics_file = proj_dir / '_workspace' / 'metrics.json'
    quality_file = proj_dir / 'data' / 'city_quality.json'  # blog-pipeline only
    
    lines = []
    # [A2-FIX] frontmatter 추가 — kb-lint frontmatter_missing WARNING 방지
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append('---')
    lines.append(f'title: "NOVA {name} KB 동기화 스냅샷"')
    lines.append(f'tags: ["nova", "project", "automation"]')
    lines.append(f'type: "project"')
    lines.append(f'created: "{today}"')
    lines.append(f'updated: "{today}"')
    lines.append(f'status: "active"')
    lines.append('---')
    lines.append('')
    lines.append(f'# NOVA {name} KB 동기화 스냅샷')
    lines.append(f'_업데이트: {datetime.now().strftime("%Y-%m-%d %H:%M")}_')
    lines.append('')
    
    # 1. evolution.md 요약 (마지막 20줄)
    if evo_file.exists():
        evo_lines = evo_file.read_text(encoding='utf-8').splitlines()
        recent = evo_lines[-20:] if len(evo_lines) > 20 else evo_lines
        lines.append('## 최근 evolution (20줄)')
        lines.extend(recent)
        lines.append('')
        lines.append(f'> 전체 {len(evo_lines)}줄 중 최근 20줄')
        lines.append('')
    
    # 2. metrics 요약 — [H-1 fix] nova.py phase_stats 필드 기준으로 수정
    if metrics_file.exists():
        try:
            m = json.loads(metrics_file.read_text())
        except Exception:
            m = {}
        lines.append('## 메트릭 요약')
        stats = m.get('phase_stats', {})
        if stats:
            total_runs    = sum(e.get('runs', 0) for e in stats.values())
            total_success = sum(e.get('success', 0) for e in stats.values())
            total_fail    = sum(e.get('fail', 0) for e in stats.values())
            lines.append(f'- 전체 실행: {total_runs}회 ({total_success}성공 / {total_fail}실패)')
            for ph in sorted(stats.keys(), key=lambda x: int(x)):
                ph_data = stats[ph]
                runs    = ph_data.get('runs', 0)
                success = ph_data.get('success', 0)
                avg     = ph_data.get('avg_sec', 0.0)   # [H-1 fix] avg_duration_s → avg_sec
                last_r  = ph_data.get('last_run', '-')
                lines.append(f'- Phase {ph}: {success}/{runs} 성공, 평균 {avg:.1f}s, 최근={last_r}')
        else:
            lines.append('- (metrics 없음 — Phase 실행 후 자동 기록)')
        lines.append('')
    
    # 3. city_quality (blog-pipeline에만)
    if quality_file.exists():
        try:
            cq = json.loads(quality_file.read_text())
        except (json.JSONDecodeError, OSError) as _e:
            lines.append(f'> city_quality.json 읽기 실패: {_e}')  # [B-09 FIX] silent fail 방지
            cq = {}
        cities = cq.get('cities', {})
        if cities:
            lines.append('## 도시별 품질점수')
            for city, data in sorted(cities.items()):
                bl = ' [블랙리스트]' if data.get('blacklisted') else ''
                lines.append(f'- {city}: 평균 {data.get("avg_score",0)}점, {data.get("runs",0)}회{bl}')
            lines.append('')
    
    return '\n'.join(lines)

def main():
    # BUG-KB-1 fix: /tmp/nova_kb_sync.lock 획득 → autonomous_engine의 lock 체크 유효화
    SYNC_LOCK = "/tmp/nova_kb_sync.lock"
    lock_fd = open(SYNC_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        print("[nova_kb_sync] 이미 실행 중 — SKIP", flush=True)
        return
    try:
        KB_DIR.mkdir(parents=True, exist_ok=True)
        updated = []
        PROJECTS = _load_all_projects()  # [M-3 fix] 동적 로드
        for name in PROJECTS:
            try:
                content = sync_project(name)
                out = KB_DIR / f'{name}-nova.md'
                # [R20-CX-001-FIX] write_text → mkstemp+replace 원자적 쓰기 (크론 동시 실행 충돌 방지)
                fd, tmp_path = tempfile.mkstemp(dir=str(KB_DIR), suffix='.md.tmp')
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                        fd = -1  # fdopen이 소유권 취득 — 이중 close 방지
                        tmp_f.write(content)
                    pathlib.Path(tmp_path).replace(out)
                    tmp_path = None
                except Exception:
                    if fd != -1:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    if tmp_path:
                        try:
                            pathlib.Path(tmp_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
                updated.append(name)
                print(f'[OK] {name} → {out}')
            except Exception as e:
                print(f'[ERR] {name}: {e}')

        # [R16-CX-003-FIX] KB_LOG append에 fcntl 배타락 적용 (크론 동시 실행 충돌 방지)
        with open(KB_LOG, 'a', encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(f"## [{datetime.now().strftime('%Y-%m-%d')}] nova-kb-sync | {', '.join(updated)} 업데이트\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        print(f'\n[nova_kb_sync] {len(updated)}/{len(PROJECTS)} 프로젝트 KB 동기화 완료')
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

if __name__ == '__main__':
    main()
