"""
nova.db.schema — NOVA Brain database DDL.

The brain.db is a lightweight SQLite store that persists:
  - pages:           indexed knowledge documents
  - takes:           atomic knowledge claims with quality weights
  - contradictions:  conflicting claims flagged for resolution
  - brain_health:    periodic health snapshots
  - events:          system events for inter-component signaling
  - kanban tasks:    optional task-tracking integration
"""

BRAIN_SCHEMA = """
-- Knowledge pages (documents / KB articles)
CREATE TABLE IF NOT EXISTS pages (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    title       TEXT,
    page_type   TEXT DEFAULT 'general',
    agent       TEXT,
    tags        TEXT,         -- JSON list
    compiled_truth TEXT,
    timeline    TEXT,
    char_count  INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Atomic knowledge claims
CREATE TABLE IF NOT EXISTS takes (
    id          TEXT PRIMARY KEY,
    page_id     TEXT REFERENCES pages(id),
    kind        TEXT DEFAULT 'fact',   -- fact | insight | lesson | pattern
    holder      TEXT,                  -- agent that produced this take
    claim       TEXT NOT NULL,
    weight      REAL DEFAULT 0.5,      -- quality score 0.0-1.0
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Contradictions between takes
CREATE TABLE IF NOT EXISTS contradictions (
    id          TEXT PRIMARY KEY,
    take_a      TEXT REFERENCES takes(id),
    take_b      TEXT REFERENCES takes(id),
    status      TEXT DEFAULT 'open',   -- open | resolved
    resolution  TEXT,
    created_at  TEXT NOT NULL
);

-- Periodic brain health snapshots
CREATE TABLE IF NOT EXISTS brain_health (
    id              TEXT PRIMARY KEY,
    score_overall   REAL,
    takes_total     INTEGER,
    orphan_count    INTEGER,
    created_at      TEXT NOT NULL
);

-- System events for signaling between watchers and engines
CREATE TABLE IF NOT EXISTS nova_events (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,         -- SYNTHESIS_DONE | DREAM_DONE | STAGNANT_AGENT | etc.
    severity    TEXT DEFAULT 'INFO',   -- INFO | HIGH | CRITICAL
    title       TEXT NOT NULL,
    detail      TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL,
    is_read     INTEGER DEFAULT 0
);

-- Graph edges (knowledge relationships between pages)
CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id          TEXT PRIMARY KEY,
    src_page_id TEXT REFERENCES pages(id),
    dst_page_id TEXT REFERENCES pages(id),
    edge_type   TEXT NOT NULL,  -- worked_on/depends_on/fixes/caused_by/referenced_by
    weight      REAL DEFAULT 0.5,
    agent       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kge_src  ON knowledge_graph_edges(src_page_id);
CREATE INDEX IF NOT EXISTS idx_kge_dst  ON knowledge_graph_edges(dst_page_id);
CREATE INDEX IF NOT EXISTS idx_kge_type ON knowledge_graph_edges(edge_type);
"""

# Kanban schema (separate DB, optional integration)
KANBAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT DEFAULT 'todo',   -- todo | ready | running | done | blocked
    agent       TEXT,
    priority    INTEGER DEFAULT 50,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""
