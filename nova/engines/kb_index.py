#!/usr/bin/env python3
import sys, os, sqlite3, hashlib
from pathlib import Path
from datetime import datetime, timezone
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes')))
NOVA_HOME   = Path(os.environ.get('NOVA_HOME',   str(Path.home()/'.nova')))
KB_ROOT  = HERMES_HOME / 'kb'
BRAIN_DB = NOVA_HOME / 'brain.db'
SKIP_FILES = {'log.md','index.md','INDEX.md','SCHEMA.md','_registry.md','TEMPLATE.md','memory_pending.md'}
SKIP_DIRS  = {'archive','weekly','__pycache__','nova_workspace'}

def run(changed_path=None):
    if not BRAIN_DB.exists(): return 0
    now = datetime.now(timezone.utc).isoformat()
    targets = [Path(changed_path)] if changed_path else list(KB_ROOT.rglob('*.md'))
    inserted = 0
    with sqlite3.connect(str(BRAIN_DB)) as con:
        con.execute('PRAGMA journal_mode=WAL')
        for md in targets:
            if md.name in SKIP_FILES: continue
            if any(p in SKIP_DIRS for p in md.parts): continue
            if not md.exists(): continue
            try: text = md.read_text(encoding='utf-8', errors='ignore')
            except: continue
            if len(text) < 10: continue
            try: rel = str(md.relative_to(KB_ROOT)).replace(chr(92),'/')
            except: rel = str(md)
            lines = text.lstrip('#').split(chr(10))
            title = (lines[0].strip() or md.stem)[:100]
            h = hashlib.md5(text.encode()).hexdigest()
            con.execute(
                'INSERT OR IGNORE INTO pages'
                '  (path,title,page_type,compiled_truth,char_count,content_hash,indexed_at,created_at,updated_at)'
                '  VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM pages WHERE path=?),?),?)'
            , (rel, title, 'kb', text[:3000], len(text), h, now, rel, now, now))
            inserted += 1
        con.commit()
    print('[kb_index]', inserted, 'pages indexed')
    return inserted

if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv)>1 else None)
