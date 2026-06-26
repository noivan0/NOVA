#!/usr/bin/env python3
"""
nova_migrate_ct.py — 기존 KB 파일을 GBrain CT+TL 구조로 마이그레이션

기존:
  # 제목
  내용 전체

변환 후:
  ---
  title: ...
  page_type: ...
  migrated_at: ...
  ---

  ## Compiled Truth
  내용 전체 (덮어쓰기 가능한 현재 최선의 이해)

  ## Timeline
  > 추가전용 증거 흔적
  - YYYY-MM-DD: [migrate] 기존 KB 파일에서 CT+TL 구조로 변환됨

실행:
  python3 nova_migrate_ct.py --dry-run          # 미리보기
  python3 nova_migrate_ct.py --batch 50         # 50개씩 변환
  python3 nova_migrate_ct.py --file kb/foo.md   # 단일 파일
"""
import os
import sys
import re
import shutil
import argparse
from pathlib import Path
from datetime import datetime, timezone

KB_ROOT   = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
BACKUP_DIR = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb_backup_ct_migration"
TODAY = datetime.now().strftime("%Y-%m-%d")

# 스킵할 파일명
SKIP_FILES = {
    "index.md", "log.md", "log-2026.md", "SCHEMA.md",
    "_registry.md", "TEMPLATE.md", "memory_pending.md",
    "DESCRIPTION.md"
}

# 스킵할 디렉토리
SKIP_DIRS = {"archive", "weekly"}

# 이미 CT 구조인지 확인
def is_ct_structure(content: str) -> bool:
    return "## Compiled Truth" in content or "## Timeline" in content

# 프론트매터 파싱
def parse_frontmatter(content: str):
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end < 0:
        return {}, content
    fm_text = content[3:end]
    body = content[end+4:].strip()
    fm = {}
    try:
        import yaml
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        pass
    return fm, body

# 페이지 타입 추론
def infer_page_type(path: Path, fm: dict) -> str:
    if fm.get("page_type"):
        return fm["page_type"]
    parts = str(path).split("/")
    if "projects" in parts:   return "project"
    if "config" in parts:     return "config"
    if "fixes" in parts:      return "fix"
    if "agents" in parts:     return "agent"
    if "user" in parts:       return "entity"
    return "general"

# CT+TL 변환
def convert_to_ct(path: Path, dry_run: bool = False) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False

    if is_ct_structure(content):
        return False  # 이미 변환됨

    fm, body = parse_frontmatter(content)
    page_type = infer_page_type(path, fm)

    # 제목 추출
    title = fm.get("title") or fm.get("name")
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = m.group(1).strip() if m else path.stem

    # 프론트매터 재구성
    agent = fm.get("agent", "")
    tags = fm.get("tags", "")
    fm_lines = [
        "---",
        f"title: {title}",
        f"page_type: {page_type}",
    ]
    if agent:
        fm_lines.append(f"agent: {agent}")
    if tags:
        fm_lines.append(f"tags: {tags}")
    fm_lines += [
        f"migrated_at: {TODAY}",
        f"migrated_from: {str(path.relative_to(KB_ROOT.parent))}",
        "---",
    ]
    fm_block = "\n".join(fm_lines)

    new_content = f"""{fm_block}

## Compiled Truth

{body.strip()}

## Timeline

> 이 섹션은 추가전용입니다. 기존 항목을 절대 수정하지 마세요.

- {TODAY}: [migrate] nova_migrate_ct.py — CT+TL 구조로 변환됨
"""

    if dry_run:
        print(f"  [DRY] {path.relative_to(KB_ROOT)}")
        print(f"        {len(content)}자 → {len(new_content)}자")
        return True

    # 실제 변환
    path.write_text(new_content, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", type=int, default=0,
                        help="한 번에 처리할 파일 수 (0=전체)")
    parser.add_argument("--file", help="단일 파일 경로")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = KB_ROOT / args.file
        ok = convert_to_ct(p, dry_run=args.dry_run)
        print(f"{'변환' if ok else '스킵'}: {p}")
        return

    # 전체 스캔
    files = []
    for f in KB_ROOT.rglob("*.md"):
        if f.name in SKIP_FILES:
            continue
        if any(d in str(f) for d in SKIP_DIRS):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if not is_ct_structure(content):
                files.append(f)
        except Exception:
            continue

    total = len(files)
    batch = args.batch if args.batch > 0 else total
    print(f"변환 대상: {total}개 (이번 배치: {batch}개)")

    if not args.dry_run and not args.no_backup and total > 0:
        # 백업
        if not BACKUP_DIR.exists():
            shutil.copytree(KB_ROOT, BACKUP_DIR,
                            ignore=shutil.ignore_patterns("*.db"))
            print(f"백업 완료: {BACKUP_DIR}")

    converted = 0
    skipped   = 0
    for f in files[:batch]:
        ok = convert_to_ct(f, dry_run=args.dry_run)
        if ok:
            converted += 1
        else:
            skipped += 1
        if converted % 50 == 0 and converted > 0:
            print(f"  진행: {converted}/{batch}")

    print(f"\n완료: 변환={converted}, 스킵={skipped}")
    if not args.dry_run and converted > 0:
        print("다음 단계: python3 $HERMES_HOME/bin/nova_brain.py index-all")


if __name__ == "__main__":
    main()
