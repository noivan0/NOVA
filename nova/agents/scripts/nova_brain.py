#!/usr/bin/env python3
"""
nova_brain.py — NOVA Brain 핵심 엔진
컴파일드 트루스 파싱, Takes 관리, 벡터 검색, 모순 감지, 헬스 측정

사용:
  from nova_brain import NovaBrain
  brain = NovaBrain()
  brain.index_kb_file("kb/agents/nova-research/foo.md")
  brain.add_take("nova-qa", "krayt", "take", "KRAYT covers OWASP Top10", weight=0.9)
  results = brain.search("security testing", top_k=5)
  brain.measure_health()

파일명 규칙 (page_type 자동 분류):
  - synth-*.md  → page_type="synthesis"  (합성 페이지)
  - weekly/     → page_type="weekly"
  - 그 외       → page_type="general"
  ⚠️ synth- prefix가 없으면 synthesis로 분류 안 됨 — 파일명 엄수 필요
"""
import sqlite3
import sqlite_vec
import json
import hashlib
import re
import struct
import numpy as np
import os
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ── 경로 상수 ─────────────────────────────────────
NOVA_BRAIN_PATH = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "nova_brain.db"
KB_ROOT         = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
EMBED_DIMS      = 3072
MAX_CHUNK_CHARS = 1500
HEALTH_THRESHOLDS = {
    "score_overall":    70.0,   # 전체 점수 < 70 → 경고
    "open_contradictions": 5,   # 모순 5개 이상 → 경고
    "stale_pages":      30,     # 90일 이상 미갱신 30개 이상 → 경고 (466페이지 기준 강화)
    "orphan_pages":     50,     # 링크 없는 페이지 50개 이상 → 경고 (982개 현실 반영)
}


# ── 임베딩 API ────────────────────────────────────
def _get_api_key() -> str:
    try:
        import yaml
        cfg = yaml.safe_load(open(Path.home() / ".hermes" / "config.yaml"))
        key = cfg.get("model", {}).get("api_key", "")
        if key.startswith("${") and key.endswith("}"):
            key = os.environ.get(key[2:-1], "")
        return key
    except Exception:
        return os.environ.get("HERMES_API_KEY", "")


def get_embedding(text: str) -> Optional[list]:
    """임베딩 엔드포인트 호출 (NOVA_EMBEDDING_BASE_URL 환경변수 필요)"""
    api_key = _get_api_key()
    if not api_key:
        return None
    embed_base_url = os.environ.get("NOVA_EMBEDDING_BASE_URL", "").rstrip("/")
    embed_model = os.environ.get("NOVA_EMBEDDING_MODEL", "text-embedding-3-large")
    if not embed_base_url:
        return None
    url = f"{embed_base_url}/{embed_model}/embeddings"
    try:
        resp = requests.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"input": text[:8000], "model": embed_model},
            timeout=30,
            verify=False
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[WARN] 임베딩 실패: {e}")
        return None


def serialize_vec(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ── KB 파일 파싱 ──────────────────────────────────
def parse_kb_file(path: Path) -> dict:
    """
    KB 파일을 파싱해서 컴파일드 트루스 / 타임라인 / 메타 분리
    
    GBrain 패턴:
      ## Compiled Truth  → compiled_truth (덮어쓰기 가능)
      ## Timeline        → timeline (추가전용)
      ## Takes           → takes 파싱
    
    기존 파일은 전체 내용을 compiled_truth로 처리
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    result = {
        "title": None,
        "agent": None,
        "page_type": "general",
        "compiled_truth": "",
        "timeline": "",
        "raw_takes": [],
        "frontmatter": {},
    }

    # YAML 프론트매터 파싱
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end > 0:
            fm_text = content[3:end]
            content_body = content[end+4:].strip()
            try:
                import yaml
                fm = yaml.safe_load(fm_text) or {}
                result["frontmatter"] = fm
                # F1 fix: YAML이 title을 list로 파싱할 수 있음 (e.g. [[wikilink]] → list)
                raw_title = fm.get("title") or fm.get("name")
                if isinstance(raw_title, list):
                    raw_title = ", ".join(str(x) for x in raw_title)
                elif raw_title is not None and not isinstance(raw_title, str):
                    raw_title = str(raw_title)
                result["title"] = raw_title
                result["agent"] = fm.get("agent")
                result["page_type"] = fm.get("page_type", "general")
            except Exception:
                pass
            content = content_body

    # page_type 자동 추론 (frontmatter 없거나 general인 경우 파일명 기반)
    if result["page_type"] == "general":
        fname = path.name
        if fname.startswith("synth-"):
            result["page_type"] = "synthesis"
        elif any(d in str(path) for d in ["weekly/", "archive/"]):
            result["page_type"] = "weekly"
        elif any(d in str(path) for d in ["audit_loop/", "audit/"]):
            result["page_type"] = "audit"
            result["agent"] = result.get("agent") or "system"
        elif "agents/" in str(path):
            # agents/nova-xxx/... 경로에서 에이전트명 자동 추출
            parts = str(path).replace("\\", "/").split("agents/")
            if len(parts) > 1:
                agent_part = parts[1].split("/")[0]
                if agent_part:
                    result["agent"] = result.get("agent") or agent_part

    # 제목 추출 (없으면 # 헤더에서)
    if not result["title"]:
        m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if m:
            result["title"] = m.group(1).strip()

    # GBrain 섹션 구조 파싱
    ct_match = re.search(r"^##\s+Compiled Truth\s*$", content, re.MULTILINE | re.IGNORECASE)
    tl_match = re.search(r"^##\s+Timeline\s*$", content, re.MULTILINE | re.IGNORECASE)
    tk_match = re.search(r"^##\s+Takes\s*$", content, re.MULTILINE | re.IGNORECASE)

    if ct_match or tl_match:
        # GBrain 구조 파일 — CT는 Timeline 이전까지 전부
        if ct_match and tl_match:
            ct_start = ct_match.end()
            tl_start = tl_match.start()
            result["compiled_truth"] = content[ct_start:tl_start].strip()
            tl_end_match = re.search(r"^##\s+Takes\s*$", content[tl_match.end():], re.MULTILINE | re.IGNORECASE)
            if tl_end_match:
                result["timeline"] = content[tl_match.end():tl_match.end() + tl_end_match.start()].strip()
            else:
                result["timeline"] = content[tl_match.end():].strip()
        elif ct_match:
            result["compiled_truth"] = content[ct_match.end():].strip()
        elif tl_match:
            result["compiled_truth"] = content[:tl_match.start()].strip()
            result["timeline"] = content[tl_match.end():].strip()

        # Takes 섹션
        if tk_match:
            result["raw_takes"] = _parse_takes(content[tk_match.end():].strip())
    else:
        # 기존 파일 → 전체를 compiled_truth로
        result["compiled_truth"] = content.strip()

    return result


def _parse_takes(text: str) -> list:
    """Takes 섹션 파싱: | kind | claim | weight | holder | source |"""
    takes = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|") or "---" in line or "kind" in line.lower():
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) >= 3:
            takes.append({
                "kind": parts[0] if parts[0] in ("fact","take","bet","hunch") else "take",
                "claim": parts[1],
                "weight": float(parts[2]) if len(parts) > 2 and parts[2] else 0.5,
                "holder": parts[3] if len(parts) > 3 else "unknown",
                "source": parts[4] if len(parts) > 4 else None,
            })
    return takes


# ── 청킹 ─────────────────────────────────────────
def chunk_text(text: str, section: str, max_chars: int = MAX_CHUNK_CHARS, min_chars: int = 150) -> list:
    """단락 단위 청킹 (min_chars 미달 chunk는 이전 chunk에 합치거나 제거)"""
    if not text:
        return []
    paragraphs = re.split(r"\n{2,}", text)
    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars and current:
            stripped = current.strip()
            if len(stripped) >= min_chars:
                chunks.append({"section": section, "content": stripped})
                current = para
            else:
                # min_chars 미달: 이전 chunk에 합치거나 계속 누적
                if chunks:
                    chunks[-1]["content"] = chunks[-1]["content"] + "\n\n" + stripped
                    current = para
                else:
                    current = current + "\n\n" + para if current else para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        stripped = current.strip()
        if len(stripped) >= min_chars:
            chunks.append({"section": section, "content": stripped})
        elif chunks:  # 짧으면 이전 chunk에 합치기
            chunks[-1]["content"] = chunks[-1]["content"] + "\n" + stripped
    return chunks


# ── NovaBrain 메인 클래스 ─────────────────────────
class NovaBrain:
    def __init__(self, path: Path = NOVA_BRAIN_PATH):
        self.path = path
        self.conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")  # 10초 (brain_watcher 경쟁 시 대기)
        conn.execute("PRAGMA cache_size=-64000")
        return conn

    def _page_id(self, path: str) -> str:
        return hashlib.sha256(path.encode()).hexdigest()[:16]

    def _chunk_id(self, page_id: str, idx: int, section: str) -> str:
        return hashlib.sha256(f"{page_id}:{idx}:{section}".encode()).hexdigest()[:16]

    def _take_id(self, page_id: str, kind: str, holder: str, claim: str,
                 source: Optional[str] = None) -> str:
        """raw_takes용 안정적 ID 생성.

        기존 구현은 Python 내장 hash()를 사용해 프로세스 재시작마다 ID가 바뀌었고,
        같은 KB를 재인덱싱할 때 중복 takes가 누적될 수 있었다.
        """
        payload = f"{page_id}:{kind}:{holder}:{claim}:{source or ''}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    # ── 인덱싱 ────────────────────────────────────
    def index_kb_file(self, kb_path: str, embed: bool = True) -> bool:
        """KB 파일 하나를 nova_brain.db에 인덱싱"""
        abs_path = KB_ROOT / kb_path if not kb_path.startswith("/") else Path(kb_path)
        if not abs_path.exists():
            return False

        parsed = parse_kb_file(abs_path)
        if not parsed:
            return False

        content_all = parsed["compiled_truth"] + "\n" + parsed["timeline"]
        content_hash = hashlib.sha256(content_all.encode()).hexdigest()[:16]
        page_id = self._page_id(kb_path)
        now = datetime.now(timezone.utc).isoformat()

        # 기존 해시 확인 (변경 없으면 스킵)
        cur = self.conn.execute("SELECT content_hash FROM pages WHERE id=?", (page_id,))
        row = cur.fetchone()
        if row and row["content_hash"] == content_hash:
            return True  # 변경 없음

        # pages 삽입/업데이트
        self.conn.execute("""
            INSERT INTO pages (id, path, title, agent, page_type, compiled_truth, timeline,
                              content_hash, char_count, indexed_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, agent=excluded.agent,
                page_type=excluded.page_type,
                compiled_truth=excluded.compiled_truth, timeline=excluded.timeline,
                content_hash=excluded.content_hash, char_count=excluded.char_count,
                updated_at=excluded.updated_at
        """, (page_id, kb_path,
              parsed.get("title") or kb_path.split("/")[-1],
              parsed.get("agent"),
              parsed.get("page_type", "general"),
              parsed["compiled_truth"],
              parsed["timeline"],
              content_hash,
              len(content_all),
              now, now))

        # chunk_vectors에서도 삭제 (연관 chunk_id들)
        old_chunks = self.conn.execute(
            "SELECT id FROM page_chunks WHERE page_id=?", (page_id,)
        ).fetchall()
        for c in old_chunks:
            self.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (c["id"],))
        # 기존 청크/벡터 삭제
        self.conn.execute("DELETE FROM page_chunks WHERE page_id=?", (page_id,))

        # 청킹 및 벡터화
        chunks = (
            chunk_text(parsed["compiled_truth"], "compiled_truth") +
            chunk_text(parsed["timeline"], "timeline")
        )
        for idx, chunk in enumerate(chunks):
            cid = self._chunk_id(page_id, idx, chunk["section"])
            self.conn.execute("""
                INSERT OR REPLACE INTO page_chunks (id, page_id, chunk_idx, section, content, char_count, indexed_at)
                VALUES (?,?,?,?,?,?,?)
            """, (cid, page_id, idx, chunk["section"], chunk["content"], len(chunk["content"]), now))

            if embed:
                vec = get_embedding(chunk["content"])
                if vec:
                    self.conn.execute("""
                        INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding)
                        VALUES (?, ?)
                    """, (cid, serialize_vec(vec)))

        # Takes 저장
        for t in parsed.get("raw_takes", []):
            tid = self._take_id(
                page_id,
                t["kind"],
                t.get("holder", "unknown"),
                t["claim"],
                t.get("source"),
            )
            self.conn.execute("""
                INSERT OR IGNORE INTO takes
                (id, page_id, kind, holder, claim, weight, source, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (tid, page_id, t["kind"], t.get("holder","unknown"),
                  t["claim"], t.get("weight", 0.5), t.get("source"), now))

        self.conn.commit()
        return True

    # ── 벡터 검색 ─────────────────────────────────
    def index_kb_file_abs(self, abs_path: str, rel_path: str, embed: bool = True,
                          agent_hint: Optional[str] = None) -> bool:
        """절대 경로 + 상대 경로를 받아 nova_brain.db에 인덱싱 (다중 KB 루트 지원)"""
        p = Path(abs_path)
        if not p.exists():
            return False
        parsed = parse_kb_file(p)
        if not parsed:
            return False

        # BUG-HIGH-3: frontmatter에 agent 없으면 agent_hint 사용
        if parsed.get("agent") is None and agent_hint:
            parsed["agent"] = agent_hint

        content_all = parsed["compiled_truth"] + "\n" + parsed["timeline"]
        content_hash = hashlib.sha256(content_all.encode()).hexdigest()[:16]
        # F2 fix: path UNIQUE 충돌 방지 — 기존 page 행은 id가 달라도 path로 찾아서 그 id 사용
        existing_by_path = self.conn.execute(
            "SELECT id, content_hash FROM pages WHERE path=?", (rel_path,)
        ).fetchone()
        if existing_by_path:
            page_id = existing_by_path["id"]  # 기존 id 재사용
            if existing_by_path["content_hash"] == content_hash:
                has_chunks = self.conn.execute(
                    "SELECT 1 FROM page_chunks WHERE page_id=? LIMIT 1", (page_id,)
                ).fetchone()
                if has_chunks:
                    return True
        else:
            page_id = self._page_id(rel_path)
            # id로 조회 (path는 없지만 id 충돌 방지)
            existing_by_id = self.conn.execute(
                "SELECT content_hash FROM pages WHERE id=?", (page_id,)
            ).fetchone()
            if existing_by_id and existing_by_id["content_hash"] == content_hash:
                has_chunks = self.conn.execute(
                    "SELECT 1 FROM page_chunks WHERE page_id=? LIMIT 1", (page_id,)
                ).fetchone()
                if has_chunks:
                    return True
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO pages (id, path, title, agent, page_type, compiled_truth, timeline,
                              content_hash, char_count, indexed_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, agent=excluded.agent,
                page_type=excluded.page_type,
                compiled_truth=excluded.compiled_truth, timeline=excluded.timeline,
                content_hash=excluded.content_hash, char_count=excluded.char_count,
                updated_at=excluded.updated_at
        """, (page_id, rel_path,
              parsed.get("title") or rel_path.split("/")[-1],
              parsed.get("agent"),
              parsed.get("page_type", "general"),
              parsed["compiled_truth"], parsed["timeline"],
              content_hash, len(content_all), now, now))

        # 기존 청크/벡터 삭제 후 재인덱싱 (index_kb_file과 동일 로직)
        old_chunks = self.conn.execute(
            "SELECT id FROM page_chunks WHERE page_id=?", (page_id,)
        ).fetchall()
        for c in old_chunks:
            self.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (c["id"],))
        self.conn.execute("DELETE FROM page_chunks WHERE page_id=?", (page_id,))

        # 청킹 및 벡터화
        chunks = (
            chunk_text(parsed["compiled_truth"], "compiled_truth") +
            chunk_text(parsed["timeline"], "timeline")
        )
        for idx, chunk in enumerate(chunks):
            cid = self._chunk_id(page_id, idx, chunk["section"])
            self.conn.execute("""
                INSERT OR REPLACE INTO page_chunks (id, page_id, chunk_idx, section, content, char_count, indexed_at)
                VALUES (?,?,?,?,?,?,?)
            """, (cid, page_id, idx, chunk["section"], chunk["content"], len(chunk["content"]), now))

            if embed:
                vec = get_embedding(chunk["content"])
                if vec:
                    self.conn.execute("""
                        INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding)
                        VALUES (?, ?)
                    """, (cid, serialize_vec(vec)))

        # Takes 저장
        for t in parsed.get("raw_takes", []):
            tid = self._take_id(
                page_id,
                t["kind"],
                t.get("holder", "unknown"),
                t["claim"],
                t.get("source"),
            )
            self.conn.execute("""
                INSERT OR IGNORE INTO takes
                (id, page_id, kind, holder, claim, weight, source, created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (tid, page_id, t["kind"], t.get("holder", "unknown"),
                  t["claim"], t.get("weight", 0.5), t.get("source"), now))

        self.conn.commit()
        return True


    def search(self, query: str, top_k: int = 10,
               section: Optional[str] = None,
               agent: Optional[str] = None) -> list:
        """하이브리드 검색: 벡터(코사인) + BM25 키워드"""
        results = []

        # 1. 벡터 검색
        vec = get_embedding(query)
        if vec:
            q_vec = serialize_vec(vec)
            sql = """
                SELECT cv.chunk_id, cv.distance,
                       pc.page_id, pc.content, pc.section,
                       p.title, p.path, p.agent, p.emotional_weight
                FROM chunk_vectors cv
                JOIN page_chunks pc ON cv.chunk_id = pc.id
                JOIN pages p ON pc.page_id = p.id
                WHERE cv.embedding MATCH ?
                  AND K = ?
            """
            params = [q_vec, top_k * 2]
            if section:
                sql += " AND pc.section = ?"
                params.append(section)
            if agent:
                sql += " AND p.agent = ?"
                params.append(agent)

            for row in self.conn.execute(sql, params):
                results.append({
                    "chunk_id": row["chunk_id"],
                    "page_id": row["page_id"],
                    "title": row["title"],
                    "path": row["path"],
                    "agent": row["agent"],
                    "content": row["content"],
                    "section": row["section"],
                    "score": 1.0 - row["distance"],
                    "source": "vector",
                    "emotional_weight": row["emotional_weight"],
                })

        # 2. BM25 키워드 (LIKE 폴백 — 간단 구현)
        keywords = [w for w in query.split() if len(w) > 2]
        if keywords:
            like_clause = " OR ".join(["pc.content LIKE ?" for _ in keywords])
            params = [f"%{k}%" for k in keywords]
            sql = f"""
                SELECT pc.id as chunk_id, pc.page_id, pc.content, pc.section,
                       p.title, p.path, p.agent,
                       ({' + '.join([f"CASE WHEN pc.content LIKE ? THEN 1 ELSE 0 END" for _ in keywords])}) as kw_score
                FROM page_chunks pc
                JOIN pages p ON pc.page_id = p.id
                WHERE ({like_clause})
                ORDER BY kw_score DESC
                LIMIT ?
            """
            kw_params = params + params + [top_k * 2]
            for row in self.conn.execute(sql, kw_params):
                existing = next((r for r in results if r["chunk_id"] == row["chunk_id"]), None)
                if existing:
                    existing["score"] += row["kw_score"] * 0.1  # BM25 부스트
                else:
                    results.append({
                        "chunk_id": row["chunk_id"],
                        "page_id": row["page_id"],
                        "title": row["title"],
                        "path": row["path"],
                        "agent": row["agent"],
                        "content": row["content"],
                        "section": row["section"],
                        "score": row["kw_score"] * 0.1,
                        "source": "keyword",
                    })

        # 소스별 부스트 (GBrain 패턴) + emotional_weight 부스트
        SOURCE_BOOST = {
            "agents": 1.5,
            "projects": 1.2,
            "config": 0.9,
            "weekly": 0.7,
        }
        for r in results:
            for src, boost in SOURCE_BOOST.items():
                if src in r.get("path", ""):
                    r["score"] *= boost
                    break

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    # ── Takes 관리 ────────────────────────────────
    def add_take(self, holder: str, page_path: str, kind: str,
                 claim: str, weight: float = 0.5,
                 source: Optional[str] = None,
                 evidence: Optional[str] = None) -> str:
        """신념/주장 추가"""
        page_id = self._page_id(page_path)
        # RISK-3 fix: holder+claim+오늘날짜 기반 ID → 같은 날 중복 claim = INSERT OR IGNORE
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        take_id = hashlib.sha256(f"{holder}:{claim}:{today}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT OR IGNORE INTO takes (id, page_id, kind, holder, claim, weight, source, evidence, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (take_id, page_id, kind, holder, claim, weight, source, evidence, now))
        self.conn.commit()
        return take_id

    def resolve_take(self, take_id: str, outcome: str, brier_score: Optional[float] = None):
        """Take 결과 판정 (bet 전용)"""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            UPDATE takes SET outcome=?, brier_score=?, updated_at=? WHERE id=?
        """, (outcome, brier_score, now, take_id))
        self.conn.commit()

    def get_takes(self, page_path: str = None, holder: str = None,
                  kind: str = None, active_only: bool = True) -> list:
        """Takes 조회"""
        sql = "SELECT * FROM takes WHERE 1=1"
        params = []
        if page_path:
            sql += " AND page_id=?"; params.append(self._page_id(page_path))
        if holder:
            sql += " AND holder=?"; params.append(holder)
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if active_only:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ── 모순 감지 ─────────────────────────────────
    def detect_contradictions(self, top_k_pairs: int = 50) -> list:
        """
        상위 유사 페이지 쌍에서 컴파일드 트루스 비교
        LLM 판단 없이 키워드 기반 빠른 감지
        같은 에이전트 또는 같은 프로젝트 내 페이지만 비교 (cross-project 오탐 방지)
        """
        # 최근 갱신된 페이지들을 대상으로
        pages = self.conn.execute("""
            SELECT id, path, title, compiled_truth, agent
            FROM pages
            WHERE compiled_truth IS NOT NULL AND length(compiled_truth) > 100
            AND path NOT LIKE '%evolution%'
            AND path NOT LIKE '%INDEX%'
            AND path NOT LIKE '%index.md'
            AND path NOT LIKE '%반복%테마%'
            AND path NOT LIKE '%patterns%'
            AND path NOT LIKE '%bugbounty%'
            AND title NOT LIKE '%반복 테마%'
            AND path NOT LIKE 'fixes/%'
            AND path NOT LIKE 'wiki/entities/%'
            AND path NOT LIKE 'wiki/concepts/%'
            ORDER BY updated_at DESC LIMIT 100
        """).fetchall()

        contradictions_found = []
        now = datetime.now(timezone.utc).isoformat()

        # 부정 패턴 쌍 감지
        negation_patterns = [
            (r"\b(is|are|has|have|does|do)\b", r"\b(is not|are not|has not|have not|does not|do not|isn't|aren't|hasn't|haven't)\b"),
            (r"\b(always|always)\b", r"\b(never|sometimes|rarely)\b"),
            (r"\b(enabled|active|running)\b", r"\b(disabled|inactive|stopped)\b"),
            (r"\b(success|passed|works)\b", r"\b(fail|failed|broken|doesn't work)\b"),
        ]

        checked = set()
        for i, p1 in enumerate(pages):
            for p2 in pages[i+1:]:
                # NULL id 방어: id가 None인 페이지는 비교 스킵 (NoneType 정렬 오류 방지)
                if p1["id"] is None or p2["id"] is None:
                    continue
                pair_key = tuple(sorted([p1["id"], p2["id"]]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                # Cross-project 오탐 방지: 다른 에이전트 + 다른 path prefix이면 스킵
                # (같은 날짜의 서로 다른 프로젝트 문서들이 구조적 유사성으로 오탐됨)
                a1 = p1["agent"] or ""
                a2 = p2["agent"] or ""
                path1 = p1["path"] or ""
                path2 = p2["path"] or ""
                # 같은 에이전트이거나, 같은 프로젝트(path prefix)인 경우만 비교
                same_agent = a1 and a2 and a1 == a2
                same_project = (path1.split("/")[0] == path2.split("/")[0]) if path1 and path2 else False
                if not same_agent and not same_project:
                    continue  # 다른 프로젝트 간 비교 스킵

                # 이미 감지(open/dismissed)된 쌍은 스킵
                existing = self.conn.execute("""
                    SELECT id FROM contradictions
                    WHERE (take_a=? AND take_b=?)
                       OR (take_a=? AND take_b=?)
                """, (p1["id"], p2["id"], p2["id"], p1["id"])).fetchone()
                if existing:
                    continue

                # 간단한 키워드 충돌 감지
                ct1 = p1["compiled_truth"] or ""
                ct2 = p2["compiled_truth"] or ""

                # 같은 엔티티에 대한 상반된 서술 감지 (간략 구현)
                nums_1 = set(re.findall(r'\b\d+(?:\.\d+)?\b', ct1))
                nums_2 = set(re.findall(r'\b\d+(?:\.\d+)?\b', ct2))

                # 공통 맥락 키워드가 있고 수치가 크게 다른 경우
                common_words = set(ct1.lower().split()) & set(ct2.lower().split())
                significant_common = [w for w in common_words
                                      if len(w) > 5 and w not in
                                      {"about","which","their","would","could","should"}]

                if len(significant_common) >= 20 and nums_1 and nums_2 and len(nums_1) >= 5:  # 높은 임계값으로 오탐 방지
                    # 같은 맥락에서 다른 수치 → 잠재적 모순
                    diff_nums = nums_1.symmetric_difference(nums_2)
                    if diff_nums and len(diff_nums) / max(len(nums_1), len(nums_2), 1) > 0.75:
                        cid = hashlib.sha256(f"{p1['id']}:{p2['id']}".encode()).hexdigest()[:16]

                        # severity 동적 판정
                        sym_diff = len(diff_nums)
                        diff_ratio = sym_diff / max(len(nums_1), len(nums_2), 1)
                        common_count = len(nums_1 & nums_2)
                        if diff_ratio >= 0.95 and common_count <= 5:
                            severity = "high"
                        elif diff_ratio >= 0.85 and common_count <= 8:
                            severity = "medium"
                        else:
                            severity = "low"

                        self.conn.execute("""
                            INSERT OR IGNORE INTO contradictions
                            (id, take_a, take_b, resolution, status, severity, created_at)
                            VALUES (?,?,?,?,?,?,?)
                        """, (cid, p1["id"], p2["id"],
                              ct1[:200], ct2[:200], severity, now))
                        contradictions_found.append({
                            "id": cid,
                            "path_a": p1["path"],
                            "path_b": p2["path"],
                            "severity": severity
                        })

                if len(contradictions_found) >= top_k_pairs:
                    break
            if len(contradictions_found) >= top_k_pairs:
                break

        self.conn.commit()
        return contradictions_found

    # ── 헬스 측정 ─────────────────────────────────
    def measure_health(self, agent: str = "nova-evaluator") -> dict:
        """브레인 헬스 점수 측정 및 기록"""
        now = datetime.now(timezone.utc).isoformat()

        total_pages = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        pages_with_takes = self.conn.execute(
            "SELECT COUNT(DISTINCT page_id) FROM takes WHERE page_id IS NOT NULL AND page_id IN (SELECT id FROM pages WHERE id IS NOT NULL)"
        ).fetchone()[0]
        open_contradictions = self.conn.execute(
            "SELECT COUNT(*) FROM contradictions WHERE status='open'"
        ).fetchone()[0]
        # severity별 가중치 (low=1, medium=3, high=7, critical=15)
        weighted_contradictions = self.conn.execute("""
            SELECT COALESCE(SUM(CASE severity
                WHEN 'critical' THEN 15 WHEN 'high' THEN 7
                WHEN 'medium' THEN 3 ELSE 1 END), 0)
            FROM contradictions WHERE status='open'
        """).fetchone()[0]

        # 고아 페이지 (에이전트 KB 없는 것)
        orphan_pages = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE agent IS NULL AND page_type='general'"
        ).fetchone()[0]

        # 오래된 페이지 (90일 이상 미갱신)
        stale_pages = self.conn.execute("""
            SELECT COUNT(*) FROM pages
            WHERE datetime(updated_at) < datetime('now', '-90 days')
        """).fetchone()[0]

        # 점수 계산
        score_coverage = min(100.0, pages_with_takes / max(total_pages, 1) * 200)
        score_consistency = max(0.0, 100.0 - weighted_contradictions * 1.0)  # 가중치 기반
        score_freshness = max(0.0, 100.0 - stale_pages / max(total_pages, 1) * 100)
        score_depth = min(100.0, (self.conn.execute(
            "SELECT AVG(char_count) FROM page_chunks"
        ).fetchone()[0] or 0) / 9)  # 900자 기준: AVG=900 → score=100 (현실적 기준 재설정, 한국어 KB 평균 반영)

        score_overall = (score_coverage * 0.25 + score_consistency * 0.35 +
                        score_freshness * 0.25 + score_depth * 0.15)

        thresholds_crossed = []
        if score_overall < HEALTH_THRESHOLDS["score_overall"]:
            thresholds_crossed.append(f"score_overall={score_overall:.1f}")
        if open_contradictions >= HEALTH_THRESHOLDS["open_contradictions"]:
            thresholds_crossed.append(f"contradictions={open_contradictions}")
        if stale_pages >= HEALTH_THRESHOLDS["stale_pages"]:
            thresholds_crossed.append(f"stale_pages={stale_pages}")

        result = {
            "measured_at": now,
            "measured_by": agent,
            "score_overall": round(score_overall, 1),
            "score_coverage": round(score_coverage, 1),
            "score_freshness": round(score_freshness, 1),
            "score_consistency": round(score_consistency, 1),
            "score_depth": round(score_depth, 1),
            "total_pages": total_pages,
            "pages_with_takes": pages_with_takes,
            "open_contradictions": open_contradictions,
            "orphan_pages": orphan_pages,
            "stale_pages": stale_pages,
            "thresholds_crossed": json.dumps(thresholds_crossed),
            "takes_total": self.conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0],
        }

        # BUG-HEALTH-EVOLUTION 수정: 직전 health와의 score_overall 변화량 계산
        # score_evolution = 현재 score_overall - 이전 score_overall (양수=개선, 음수=악화)
        prev_row = self.conn.execute(
            "SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        result["score_evolution"] = round(score_overall - (prev_row[0] if prev_row else score_overall), 2)

        self.conn.execute("""
            INSERT INTO brain_health
            (measured_at, measured_by, score_overall, score_coverage, score_freshness,
             score_consistency, score_depth, score_evolution, total_pages, pages_with_takes,
             open_contradictions, orphan_pages, stale_pages, takes_total, thresholds_crossed)
            VALUES (:measured_at,:measured_by,:score_overall,:score_coverage,:score_freshness,
                    :score_consistency,:score_depth,:score_evolution,:total_pages,:pages_with_takes,
                    :open_contradictions,:orphan_pages,:stale_pages,:takes_total,:thresholds_crossed)
        """, result)
        self.conn.commit()
        return result

    # ── 궤적 추적 ─────────────────────────────────
    def record_metric(self, page_path: str, metric: str, value: float,
                      unit: str = None, period: str = None, source: str = None):
        """엔티티 시계열 메트릭 기록 (GBrain 궤적 추적)"""
        page_id = self._page_id(page_path)
        now = datetime.now(timezone.utc).isoformat()
        period = period or datetime.now(timezone.utc).strftime("%Y-%m-%d")  # UTC 기준
        self.conn.execute("""
            INSERT INTO trajectories (page_id, metric, value, unit, period, source, recorded_at)
            VALUES (?,?,?,?,?,?,?)
        """, (page_id, metric, value, unit, period, source, now))
        self.conn.commit()

    def get_trajectory(self, page_path: str, metric: str, days: int = 30) -> list:
        """메트릭 시계열 조회"""
        page_id = self._page_id(page_path)
        return [dict(r) for r in self.conn.execute("""
            SELECT period, value, unit, source, recorded_at
            FROM trajectories
            WHERE page_id=? AND metric=?
              AND datetime(recorded_at) > datetime('now', ? || ' days')
            ORDER BY period ASC
        """, (page_id, metric, f"-{days}")).fetchall()]

    # ── 에이전트 활동 로그 ────────────────────────
    def log_activity(self, agent: str, action: str,
                     target_path: str = None, task_id: str = None,
                     summary: str = None, tokens: int = 0, duration: float = 0):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO agent_activity
            (agent, task_id, action, target_path, summary, tokens_used, duration_s, recorded_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (agent, task_id, action, target_path, summary, tokens, duration, now))
        self.conn.commit()

    def stats(self) -> dict:
        """DB 통계 요약"""
        return {
            "pages": self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "chunks": self.conn.execute("SELECT COUNT(*) FROM page_chunks").fetchone()[0],
            "vectors": self.conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0],
            "takes": self.conn.execute("SELECT COUNT(*) FROM takes WHERE superseded_by IS NULL").fetchone()[0],
            "contradictions_open": self.conn.execute(
                "SELECT COUNT(*) FROM contradictions WHERE status='open'"
            ).fetchone()[0],
            "latest_health": dict(self.conn.execute(
                "SELECT score_overall, measured_at FROM brain_health ORDER BY measured_at DESC LIMIT 1"
            ).fetchone() or {}),
            "agent_activities": self.conn.execute("SELECT COUNT(*) FROM agent_activity").fetchone()[0],
        }

    def close(self):
        self.conn.close()


# ── CLI ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    brain = NovaBrain()

    if len(sys.argv) < 2:
        s = brain.stats()
        print("=== NOVA Brain Stats ===")
        for k, v in s.items():
            print(f"  {k}: {v}")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "index-all":
        # 전체 KB 인덱싱
        kb_root = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "kb"
        files = list(kb_root.rglob("*.md"))
        skip = {"index.md", "log.md", "log-2026.md", "SCHEMA.md",
                "_registry.md", "TEMPLATE.md", "memory_pending.md",
                "DESCRIPTION.md"}  # skills DESCRIPTION도 스킵
        count = 0
        for f in files:
            if f.name in skip or "archive" in str(f):
                continue
            rel = str(f.relative_to(kb_root.parent))
            # nova_brain은 KB_ROOT 기준 상대경로
            rel_from_kb = str(f.relative_to(kb_root))
            ok = brain.index_kb_file(rel_from_kb, embed=False)  # 임베딩은 별도
            if ok:
                count += 1
                if count % 50 == 0:
                    print(f"  인덱싱: {count}/{len(files)}")
        print(f"완료: {count}개 파일")

    elif cmd == "search":
        query = " ".join(sys.argv[2:])
        results = brain.search(query, top_k=5)
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r['score']:.3f}] {r['title']} ({r['path']})")
            print(f"   {r['content'][:100]}...")

    elif cmd == "health":
        h = brain.measure_health()
        sc = h['score_coverage']
        tp = h['total_pages']
        pwt = h['pages_with_takes']
        real_pct = (pwt / max(tp, 1)) * 100
        print(f"Overall: {h['score_overall']}/100")
        print(f"  Coverage: {sc:.1f}(내부×200) / 실커버리지={real_pct:.1f}%({pwt}/{tp})")
        print(f"  Freshness: {h['score_freshness']:.1f}")
        print(f"  Consistency: {h['score_consistency']:.1f}")
        print(f"  Pages: {tp} | Takes: {pwt} | Contradictions: {h['open_contradictions']}")
        if h['thresholds_crossed']:
            print(f"  ⚠ ALERTS: {h['thresholds_crossed']}")

    elif cmd == "contradictions":
        found = brain.detect_contradictions()
        print(f"모순 감지: {len(found)}개")
        for c in found[:5]:
            print(f"  {c['path_a']} ↔ {c['path_b']} [{c['severity']}]")

    brain.close()
