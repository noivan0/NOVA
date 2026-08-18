#!/usr/bin/env python3
"""
nova_brain_embed.py — 기존 embeddings.db 벡터를 nova_brain.db로 마이그레이션
+ 신규 KB 파일 임베딩 자동 연동

사용:
  python3 nova_brain_embed.py --migrate   # embeddings.db → nova_brain.db
  python3 nova_brain_embed.py --sync      # 신규/변경 파일만 임베딩
  python3 nova_brain_embed.py --stats
"""
import sqlite3
import sqlite_vec
import struct
import json
import os
import sys
import argparse
from pathlib import Path

NOVA_BRAIN_PATH  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "nova_brain.db"
EMBEDDINGS_PATH  = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "embeddings.db"
KB_ROOT          = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"


def get_nova_conn():
    conn = sqlite3.connect(str(NOVA_BRAIN_PATH))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate_from_embeddings_db():
    """기존 embeddings.db의 벡터를 nova_brain.db chunk_vectors로 마이그레이션"""
    old_conn = sqlite3.connect(str(EMBEDDINGS_PATH))
    new_conn = get_nova_conn()

    cur = old_conn.execute("""
        SELECT id, path, title, content_hash, embedding, char_count
        FROM kb_embeddings
        WHERE embedding IS NOT NULL
    """)
    rows = cur.fetchall()
    print(f"마이그레이션 대상: {len(rows)}개")

    migrated = 0
    skipped  = 0
    for row in rows:
        path_str = row[1]
        embedding_json = row[3]
        embedding_raw  = row[4]

        # nova_brain.db에 해당 page의 chunk 찾기
        # path는 kb/ 로 시작하거나 없을 수 있음
        rel_path = path_str.replace("kb/", "", 1) if path_str.startswith("kb/") else path_str
        page = new_conn.execute(
            "SELECT id FROM pages WHERE path=? OR path=?",
            (rel_path, path_str)
        ).fetchone()

        if not page:
            skipped += 1
            continue

        page_id = page[0]
        # 첫 번째 chunk에 벡터 할당 (chunk_idx=0)
        chunk = new_conn.execute(
            "SELECT id FROM page_chunks WHERE page_id=? ORDER BY chunk_idx LIMIT 1",
            (page_id,)
        ).fetchone()

        if not chunk:
            skipped += 1
            continue

        chunk_id = chunk[0]

        # 이미 벡터 있으면 스킵
        exists = new_conn.execute(
            "SELECT chunk_id FROM chunk_vectors WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        if exists:
            skipped += 1
            continue

        # 임베딩 역직렬화 (JSON text 형식)
        try:
            if embedding_raw:
                vec = json.loads(embedding_raw)
            else:
                skipped += 1
                continue
            if not vec or len(vec) == 0:
                skipped += 1
                continue
        except Exception:
            skipped += 1
            continue

        # float32 직렬화 (3072차원 보장)
        if len(vec) != 3072:
            skipped += 1
            continue
        vec_bytes = struct.pack(f"{len(vec)}f", *vec)
        new_conn.execute(
            "INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding) VALUES (?,?)",
            (chunk_id, vec_bytes)
        )
        migrated += 1

        if migrated % 100 == 0:
            new_conn.commit()
            print(f"  마이그레이션: {migrated}/{len(rows)}")

    new_conn.commit()
    old_conn.close()
    new_conn.close()
    print(f"완료: 마이그레이션={migrated}, 스킵={skipped}")


def sync_new_embeddings():
    """벡터 없는 chunk들을 임베딩 API로 채우기"""
    import requests, os, yaml

    # API 키
    try:
        cfg = yaml.safe_load(open(Path.home() / ".hermes" / "config.yaml"))
        key = cfg.get("model", {}).get("api_key", "")
        if key.startswith("${") and key.endswith("}"):
            key = os.environ.get(key[2:-1], "")
    except Exception:
        key = os.environ.get("HERMES_API_KEY", "")

    conn = get_nova_conn()
    # 벡터 없는 chunk 조회
    chunks_without_vec = conn.execute("""
        SELECT pc.id, pc.content
        FROM page_chunks pc
        LEFT JOIN chunk_vectors cv ON pc.id = cv.chunk_id
        WHERE cv.chunk_id IS NULL
        LIMIT 500
    """).fetchall()

    # --batch 옵션으로 1회 처리 한도 제한
    import argparse as _ap
    _parser = _ap.ArgumentParser(add_help=False)
    _parser.add_argument("--batch", type=int, default=500)
    _args, _ = _parser.parse_known_args()
    chunks_without_vec = chunks_without_vec[:_args.batch]

    print(f"임베딩 대상: {len(chunks_without_vec)}개")
    embedded = 0

    for chunk in chunks_without_vec:
        chunk_id, content = chunk[0], chunk[1]
        if not content or len(content) < 10:
            continue

        embed_url = os.environ.get("NOVA_EMBEDDING_URL", "")
        if not embed_url:
            print("[ERROR] 환경변수 NOVA_EMBEDDING_URL 미설정 — .env 또는 nova.yaml에서 설정 필요 (임베딩 엔드포인트 URL)")
            break

        try:
            resp = requests.post(
                embed_url,
                headers={"api-key": key, "Content-Type": "application/json"},
                json={"input": content[:8000], "model": "text-embedding-3-large"},
                timeout=30,
                # P1 fix (2026-08-18, Codex-audited): unconditional
                # verify=False disabled TLS verification for every user;
                # opt out explicitly via NOVA_DISABLE_SSL_VERIFY=1 for a
                # self-signed internal gateway.
                verify=os.environ.get("NOVA_DISABLE_SSL_VERIFY", "").strip().lower()
                not in ("1", "true", "yes", "on")
            )
            resp.raise_for_status()
            vec = resp.json()["data"][0]["embedding"]
            vec_bytes = struct.pack(f"{len(vec)}f", *vec)
            conn.execute(
                "INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding) VALUES (?,?)",
                (chunk_id, vec_bytes)
            )
            embedded += 1
            if embedded % 50 == 0:
                conn.commit()
                print(f"  임베딩: {embedded}/{len(chunks_without_vec)}")
        except Exception as e:
            print(f"  [WARN] {chunk_id}: {e}")

    conn.commit()
    conn.close()
    print(f"임베딩 완료: {embedded}개")


def show_stats():
    conn = get_nova_conn()
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM page_chunks").fetchone()[0]
    vectors = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    takes = conn.execute("SELECT COUNT(*) FROM takes WHERE superseded_by IS NULL").fetchone()[0]
    health = conn.execute(
        "SELECT score_overall, measured_at FROM brain_health ORDER BY measured_at DESC LIMIT 1"
    ).fetchone()
    print(f"nova_brain.db 통계:")
    print(f"  pages:   {pages}")
    print(f"  chunks:  {chunks}")
    print(f"  vectors: {vectors} ({vectors*100//max(chunks,1)}% coverage)")
    print(f"  takes:   {takes}")
    if health:
        print(f"  health:  {health[0]}/100 ({health[1][:10]})")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true", help="embeddings.db → nova_brain.db 마이그레이션")
    parser.add_argument("--sync", action="store_true", help="벡터 없는 청크 임베딩")
    parser.add_argument("--stats", action="store_true", help="통계 출력")
    args = parser.parse_args()

    if args.migrate:
        migrate_from_embeddings_db()
    if args.sync:
        sync_new_embeddings()
    if args.stats or not any([args.migrate, args.sync]):
        show_stats()
