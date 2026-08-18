"""
syscall.py — NOVA Kernel API

에이전트가 brain.db에 직접 접근하지 않고 이 API를 통해서만 조작하도록 하는
게이트웨이 모듈. 소유권 검증 후 SQLite에 직접 접근한다.

사용 예:
    from nova.kernel.syscall import get_kernel
    k = get_kernel()
    k.kb_write("workspace/code_implement/foo.md", "내용", agent="nova-dev")
    results = k.kb_read("검색어", agent="nova-dev")

범용성:
    - HMG 전용 URL 없음. 엔드포인트는 nova.yaml 또는 환경변수에서 주입.
    - ownership.yaml 교체만으로 다른 조직에서 즉시 사용 가능.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nova.kernel.ownership import OwnershipRules


# ── 커스텀 예외 ──────────────────────────────────────────────────────────────

class NovaSyscallError(Exception):
    """NOVA Kernel 시스템 콜 기본 예외."""


class NovaPermissionError(NovaSyscallError):
    """소유권 검증 실패 시 발생하는 예외."""


# ── RunHandle — Phase 2 interrupt hook 포인트 확보 ───────────────────────────

@dataclasses.dataclass
class RunHandle:
    """spawn() 반환 핸들. Phase 2에서 interrupt/status 기능이 추가된다."""
    run_id: str

    def status(self) -> dict:
        """실행 상태 조회 (Phase 2 구현 예정)."""
        return {"run_id": self.run_id, "status": "spawned"}

    def interrupt(self, signal: str = "SIGTERM") -> bool:
        """실행 중단 신호 (Phase 2 구현 예정)."""
        raise NotImplementedError("interrupt()는 Phase 2에서 구현됩니다.")


# ── BrainSnapshot — brain_watcher 단일 쿼리 진단 ────────────────────────────

@dataclasses.dataclass
class BrainSnapshot:
    """brain_watcher 반응 트리거에 필요한 상태를 단일 쿼리로 반환."""
    takes: int
    orphan: int
    health: float


# ── KernelAPI ────────────────────────────────────────────────────────────────

class KernelAPI:
    """NOVA Agent OS Kernel API.

    brain_db 에 직접 접근하지 않고 에이전트가 이 클래스를 통해서만
    KB / Takes / Spawn 조작을 수행한다.

    Parameters
    ----------
    brain_db:
        brain.db 경로 (절대 또는 ~/... 형태 모두 허용)
    ownership_yaml:
        소유권 규칙 파일 경로. None 이면 kernel/ownership.yaml 기본값 사용.
    """

    def __init__(
        self,
        brain_db: str,
        ownership_yaml: Optional[str] = None,
    ) -> None:
        self._db_path = str(Path(brain_db).expanduser().resolve())
        self._rules = OwnershipRules(yaml_path=ownership_yaml)
        self._write_lock = threading.Lock()  # 단일 writer 보장 (WAL 충돌 방지)

    @classmethod
    def from_config(cls) -> "KernelAPI":
        """환경변수/nova.yaml 기반 기본 설정으로 인스턴스 생성.

        I-2 수정: 빈 NOVA_HOME 환경변수 방어 — 빈 문자열이면 ~/.nova 기본값 사용.
        """
        import os
        nova_home_str = (os.environ.get("NOVA_HOME") or "~/.nova").strip() or "~/.nova"
        nova_home = Path(nova_home_str).expanduser().resolve()
        brain_db  = str(nova_home / "brain.db")
        kernel_dir = Path(__file__).parent
        ownership_yaml = str(kernel_dir / "ownership.yaml") if (kernel_dir / "ownership.yaml").exists() else None
        return cls(brain_db=brain_db, ownership_yaml=ownership_yaml)

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """brain.db 에 연결 (WAL 모드, busy_timeout 10초)."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _page_id(path: str) -> str:
        """nova_brain.py 와 동일한 방식으로 page_id 생성 (sha256[:16])."""
        return hashlib.sha256(path.encode()).hexdigest()[:16]

    @staticmethod
    def _now() -> str:
        """UTC ISO8601 타임스탬프."""
        return datetime.now(timezone.utc).isoformat()

    # ── KB 읽기 ──────────────────────────────────────────────────────────────

    def kb_read(
        self,
        query: str,
        agent: str,
        limit: int = 5,
        tier: str = "auto",          # Phase 3: "hot"|"warm"|"cold"|"auto"
        after: Optional[str] = None, # ISO8601 시간 필터 (Phase 3)
        before: Optional[str] = None,
    ) -> list[dict]:
        """KB BM25 전문 검색.

        모든 에이전트에게 읽기 권한이 허용된다.
        FTS5 인덱스가 없을 경우 LIKE 폴백 검색을 수행한다.

        Phase 3: tier 파라미터로 hot/warm/cold 계층 필터를 자동 적용한다.
        after/before가 명시된 경우 tier 변환을 건너뛰고 명시값을 우선한다.

        Parameters
        ----------
        query:
            검색 쿼리 문자열
        agent:
            요청 에이전트 이름 (로깅 및 감사 목적)
        limit:
            최대 반환 수 (기본 5)
        tier:
            메모리 계층 필터. ``"hot"`` | ``"warm"`` | ``"cold"`` | ``"auto"``
            ``"auto"`` (기본) 는 시간 필터 없이 전체 검색.
        after:
            ISO8601 시간 하한. pages.updated_at > after. tier와 병용 가능.
        before:
            ISO8601 시간 상한. pages.updated_at <= before. tier와 병용 가능.

        Returns
        -------
        list[dict]
            [{page_id, path, title, agent, content, score}] 형태의 결과 목록
        """
        # ── Phase 3: tier → after/before 변환 ──────────────────────────────
        # after/before가 명시되지 않은 경우에만 tier 변환 적용
        if tier not in (None, "auto") and after is None and before is None:
            from nova.kernel.memory import MemoryLayer  # 지연 임포트 (순환 방지)
            layer  = MemoryLayer(self._db_path)
            bounds = layer.tier_bounds(tier)            # type: ignore[arg-type]
            after  = bounds.get("after")
            before = bounds.get("before")

        with self._connect() as conn:
            # FTS5 pages_fts 테이블이 있으면 BM25 검색
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            # ── Phase 3: after/before 시간 필터 조건 구성 ──────────────────
            time_conditions: list[str] = []
            time_params_prefix: list[str] = []
            if after is not None:
                time_conditions.append("p.updated_at > ?")
                time_params_prefix.append(after)
            if before is not None:
                time_conditions.append("p.updated_at <= ?")
                time_params_prefix.append(before)
            time_where = (
                " AND " + " AND ".join(time_conditions)
                if time_conditions else ""
            )

            if "pages_fts" in tables:
                fts_params = [query] + time_params_prefix + [limit]
                rows = conn.execute(
                    f"""
                    SELECT p.id, p.path, p.title, p.agent,
                           p.compiled_truth AS content,
                           bm25(pages_fts) AS score
                    FROM pages_fts
                    JOIN pages p ON pages_fts.rowid = p.rowid
                    WHERE pages_fts MATCH ?{time_where}
                    ORDER BY score
                    LIMIT ?
                    """,
                    fts_params,
                ).fetchall()
            else:
                # 폴백: LIKE 검색
                like = f"%{query}%"
                # LIKE 조건: compiled_truth/title/path 중 하나 매칭
                like_where = (
                    "(compiled_truth LIKE ? OR title LIKE ? OR path LIKE ?)"
                )
                like_params = [like, like, like] + time_params_prefix + [limit]
                rows = conn.execute(
                    f"""
                    SELECT id, path, title, agent,
                           compiled_truth AS content,
                           1.0 AS score
                    FROM pages
                    WHERE {like_where}{time_where.replace('p.', '')}
                    LIMIT ?
                    """,
                    like_params,
                ).fetchall()

        return [
            {
                "page_id": r["id"],
                "path": r["path"],
                "title": r["title"],
                "agent": r["agent"],
                "content": r["content"],
                "score": r["score"],
            }
            for r in rows
        ]

    # ── 경로 유효성 검증 ─────────────────────────────────────────────────────

    # 허용 루트 접두어 (ownership.yaml rules와 동기화 유지)
    _ALLOWED_ROOTS = (
        "workspace/", "kb/", "nova_workspace/",
        "projects/", "weekly/", "config/", "fixes/",
        "agents/", "user/", "lessons/", "memory_archive/",
    )
    _MAX_PATH_LEN = 512

    @classmethod
    def _validate_path(cls, path: str) -> str:
        """경로 정규화 + 탈출 방지 (CRITICAL C-1/C-2 수정).

        - 빈 문자열 / 길이 초과 (DoS 방지)
        - 정규화 후 ../ 포함 (path traversal 방지)
        - 절대 경로 금지
        - 허용 루트 화이트리스트
        """
        import posixpath
        if not path:
            raise NovaSyscallError("경로가 비어있습니다.")
        if len(path) > cls._MAX_PATH_LEN:
            raise NovaSyscallError(
                f"경로 길이 초과: {len(path)} > {cls._MAX_PATH_LEN}"
            )
        normalized = posixpath.normpath(path)
        if normalized.startswith("..") or normalized.startswith("/"):
            raise NovaSyscallError(
                f"경로 탈출 금지: {path!r} -> {normalized!r}"
            )
        if not any(normalized.startswith(r) for r in cls._ALLOWED_ROOTS):
            raise NovaSyscallError(
                f"허용되지 않은 경로 루트: {normalized!r}"
            )
        return normalized

    # ── KB 쓰기 ──────────────────────────────────────────────────────────────

    def kb_write(
        self,
        path: str,
        content: str,
        agent: str,
        page_type: str = "general",
        title: str = "",
    ) -> str:
        """KB 페이지 INSERT / UPDATE.

        소유권 규칙 위반 시 NovaPermissionError 를 raise 한다.
        page_id 는 path 의 sha256[:16] 으로 자동 생성된다.

        Parameters
        ----------
        path:
            KB 경로 (예: "workspace/code_implement/foo.md")
        content:
            페이지 본문 (compiled_truth 컬럼에 저장)
        agent:
            요청 에이전트 이름 (소유권 검증 대상)
        page_type:
            페이지 유형 (기본 "general")
        title:
            페이지 제목 (비어 있으면 path 의 파일명 사용)

        Returns
        -------
        str
            생성/갱신된 page_id
        """
        path = self._validate_path(path)                   # C-1/C-2: 경로 검증
        if not self._rules.can_write(path, agent):
            raise NovaPermissionError(
                f"에이전트 '{agent}' 는 경로 '{path}' 에 쓰기 권한이 없습니다."
            )

        page_id = self._page_id(path)
        assigned_agent = self._rules.assign_agent(path, agent)
        effective_title = title or Path(path).name
        now = self._now()
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        char_count = len(content)

        with self._write_lock:                              # I-1: write_lock 일관성
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO pages
                        (id, path, title, page_type, agent,
                         compiled_truth, content_hash, char_count,
                         indexed_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        compiled_truth = excluded.compiled_truth,
                        content_hash   = excluded.content_hash,
                        char_count     = excluded.char_count,
                        agent          = excluded.agent,
                        title          = excluded.title,
                        page_type      = excluded.page_type,
                        updated_at     = excluded.updated_at
                    """,
                    (
                        page_id, path, effective_title, page_type,
                        assigned_agent, content, content_hash,
                        char_count, now, now,
                    ),
                )
                conn.commit()

        return page_id

    # ── KB 삭제 ──────────────────────────────────────────────────────────────

    def kb_delete(self, path: str, agent: str) -> bool:
        """KB 페이지 삭제.

        소유자만 허용. 소유권 위반 시 NovaPermissionError.

        Returns
        -------
        bool
            True 이면 삭제 성공, False 이면 해당 경로 없음.
        """
        path = self._validate_path(path)                   # C-1/C-2: 경로 검증
        if not self._rules.can_delete(path, agent):
            raise NovaPermissionError(
                f"에이전트 '{agent}' 는 경로 '{path}' 에 삭제 권한이 없습니다."
            )

        page_id = self._page_id(path)
        with self._write_lock:                              # I-1: write_lock 일관성
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
                conn.commit()
                return cur.rowcount > 0

    # ── Takes 기록 ───────────────────────────────────────────────────────────

    def take_write(
        self,
        claim: str,
        kind: str,
        agent: str,
        weight: float = 0.5,            # nova_brain.py 호환 (health 계산)
        holder: Optional[str] = None,   # 주장 보유자 (None이면 agent)
        source: Optional[str] = None,   # 출처 (세션 ID 등)
    ) -> str:
        """Takes 테이블에 에이전트 주장(claim) 기록.

        모든 에이전트가 허용된다. take_id 는 UUID4 로 자동 생성된다.

        Parameters
        ----------
        claim:
            주장 내용 문자열
        kind:
            주장 종류 (예: "fact", "insight", "lesson", "pattern")
        agent:
            기록 에이전트 이름

        Returns
        -------
        str
            생성된 take_id (UUID4)
        """
        take_id = str(uuid.uuid4())
        now = self._now()
        effective_holder = holder or agent

        with self._write_lock:                              # I-1: write_lock 일관성
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO takes (id, kind, holder, claim, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (take_id, kind, effective_holder, claim, now, now),
                )
                conn.commit()

        return take_id

    # ── Spawn ─────────────────────────────────────────────────────────────────

    def spawn(self, harness: str, task: str, agent: str) -> RunHandle:
        """하네스 실행 요청 기록.

        nova_events 테이블에 실행 요청을 기록하고 run_id 를 반환한다.
        실제 실행은 별도의 하네스 런처가 담당한다.

        Parameters
        ----------
        harness:
            실행할 하네스 이름 (예: "nova-dev-harness")
        task:
            수행할 작업 설명
        agent:
            요청 에이전트 이름

        Returns
        -------
        str
            run_id (UUID4)
        """
        run_id = str(uuid.uuid4())
        now = self._now()

        with self._write_lock:
            with self._connect() as conn:
                # nova_events 실제 스키마: id, event_type, severity, title, detail, source, created_at, is_read, source_agent
                conn.execute(
                    """
                    INSERT OR IGNORE INTO nova_events
                        (id, event_type, severity, title, detail, source_agent, is_read, created_at)
                    VALUES (?, 'spawn', 'INFO', ?, ?, ?, 0, ?)
                    """,
                    (run_id, f"spawn:{harness}", task, agent, now),
                )
                conn.commit()

        return RunHandle(run_id=run_id)

    # ── KB 배치 쓰기 — nova_kb_sync.py 성능 유지 ──────────────────────────────

    def kb_write_batch(self, items: list[dict]) -> list[str]:
        """배치 upsert — 단일 트랜잭션으로 성능 유지.

        nova_kb_sync.py처럼 다수 파일을 처리할 때 사용.
        각 item은 kb_write() 파라미터와 동일한 키를 가진다.

        Returns
        -------
        list[str]
            생성/갱신된 page_id 목록 (순서 보장)
        """
        page_ids: list[str] = []
        with self._write_lock:
            with self._connect() as conn:
                for item in items:
                    # SECURITY-005 (2026-08-18, deep audit): kb_write_batch()
                    # never called _validate_path(), unlike kb_write()/
                    # kb_delete(). can_write() alone is insufficient because
                    # ownership.yaml glob patterns (e.g. "workspace/**") are
                    # matched against the RAW unnormalized string — a path
                    # like "workspace/../../../etc/cron.d/evil" still starts
                    # with "workspace/" and matches, then gets written to
                    # the DB verbatim with the traversal intact (reproduced:
                    # an agent with only "workspace/**" write access could
                    # persist an unnormalized ../-escaping path via this
                    # method alone, bypassing the exact same input kb_write()
                    # correctly rejects). Apply the same path validation as
                    # kb_write() before the permission check, using the
                    # normalized path for both can_write() and storage so
                    # the two APIs enforce identical policy.
                    path       = self._validate_path(item["path"])
                    content    = item["content"]
                    agent      = item["agent"]
                    page_type  = item.get("page_type", "general")
                    title      = item.get("title", "") or Path(path).name

                    if not self._rules.can_write(path, agent):
                        raise NovaPermissionError(
                            f"에이전트 '{agent}' 는 경로 '{path}' 에 쓰기 권한이 없습니다."
                        )
                    page_id        = self._page_id(path)
                    assigned_agent = self._rules.assign_agent(path, agent)
                    now            = self._now()
                    content_hash   = hashlib.sha256(content.encode()).hexdigest()

                    conn.execute(
                        """
                        INSERT INTO pages
                            (id, path, title, page_type, agent,
                             compiled_truth, content_hash, char_count,
                             indexed_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            compiled_truth = excluded.compiled_truth,
                            content_hash   = excluded.content_hash,
                            char_count     = excluded.char_count,
                            agent          = excluded.agent,
                            updated_at     = excluded.updated_at
                        """,
                        (page_id, path, title, page_type, assigned_agent,
                         content, content_hash, len(content), now, now),
                    )
                    page_ids.append(page_id)
                conn.commit()
        return page_ids

    # ── BrainSnapshot — brain_watcher 단일 쿼리 진단 ─────────────────────────

    def brain_snapshot(self) -> BrainSnapshot:
        """brain_watcher 반응 트리거에 필요한 상태를 단일 쿼리로 반환.

        3번의 개별 쿼리 대신 1번의 배치 쿼리로 latency 최소화.
        """
        with self._connect() as conn:
            takes  = conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0]
            orphan = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE agent IS NULL"
            ).fetchone()[0]
            h_row  = conn.execute(
                "SELECT score_overall FROM brain_health ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            health = float(h_row[0]) if h_row else 0.0
        return BrainSnapshot(takes=takes, orphan=orphan, health=health)

    # ── 소유권 확인 ──────────────────────────────────────────────────────────

    def check_permission(
        self,
        path: str,
        agent: str,
        op: str = "write",
    ) -> bool:
        """소유권 확인 (읽기 전용).

        Parameters
        ----------
        path:
            확인할 KB 경로
        agent:
            에이전트 이름
        op:
            작업 종류 ('read' | 'write' | 'delete')

        Returns
        -------
        bool
            허용 여부
        """
        if op == "read":
            return self._rules.can_read(path, agent)
        elif op == "write":
            return self._rules.can_write(path, agent)
        elif op == "delete":
            return self._rules.can_delete(path, agent)
        else:
            raise NovaSyscallError(f"알 수 없는 op: '{op}'. 허용값: read | write | delete")


# ── 싱글턴 접근자 — nova_bridge 교체 한 줄로 가능 ──────────────────────────

_kernel_instance: Optional[KernelAPI] = None
_kernel_lock = threading.Lock()


def get_kernel(
    brain_db: Optional[str] = None,
    ownership_yaml: Optional[str] = None,
) -> KernelAPI:
    """KernelAPI 싱글턴 반환 (I-2 수정: 교체 방어).

    최초 호출 시 from_config()로 인스턴스 생성.
    brain_db 명시 시 전역 싱글턴 교체 대신 새 인스턴스 반환 — 테스트 격리 안전.

    사용 예:
        from nova.kernel.syscall import get_kernel
        k = get_kernel()
        k.kb_write("workspace/research/out.md", "...", agent="nova-research")

    테스트에서는 get_kernel(brain_db=...) 대신 KernelAPI(brain_db=...) 직접 사용 권장.
    """
    global _kernel_instance
    with _kernel_lock:
        if brain_db is not None:
            # I-2 수정: 싱글턴 교체 금지 — 새 인스턴스 반환 (전역 오염 방지)
            import warnings
            warnings.warn(
                "get_kernel(brain_db=...) 는 새 인스턴스를 반환합니다 (싱글턴 교체 안 함). "
                "테스트에서는 KernelAPI(brain_db=...) 를 직접 사용하세요.",
                RuntimeWarning, stacklevel=2,
            )
            return KernelAPI(brain_db=brain_db, ownership_yaml=ownership_yaml)
        if _kernel_instance is None:
            _kernel_instance = KernelAPI.from_config()
    return _kernel_instance
