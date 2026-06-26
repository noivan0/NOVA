#!/usr/bin/env bash
# =============================================================================
# NOVA Full Autonomous Setup
# https://github.com/noivan0/NOVA
#
# 누구나 실행 가능한 완전자율화 설치 스크립트
# Tested on: Ubuntu 22.04+, Debian 12+, macOS 14+
# =============================================================================
set -euo pipefail

NOVA_VERSION="1.4.0"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
NOVA_REPO="https://github.com/noivan0/NOVA"
PYTHON="${PYTHON:-python3}"

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   NOVA Autonomous Agent System v${NOVA_VERSION}  ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "HERMES_HOME = $HERMES_HOME"
echo ""

# ---------------------------------------------------------------------------
# 0. 사전 요건 확인
# ---------------------------------------------------------------------------
check_req() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' not found. Please install it first."; exit 1; }
}
check_req python3
check_req pip3
check_req git
check_req sqlite3

PYTHON_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
REQUIRED_MAJOR=3
REQUIRED_MINOR=10

if $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"; then
  echo "✅ Python $PYTHON_VERSION"
else
  echo "ERROR: Python 3.10+ required (found $PYTHON_VERSION)"
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. 디렉토리 구조 생성
# ---------------------------------------------------------------------------
echo ""
echo "▶ Creating HERMES_HOME directory structure..."

mkdir -p "$HERMES_HOME"/{bin,scripts,kb,logs,profiles,kanban/boards,wiki,ipc,nova}

# 심볼릭 링크 — nova/agents/bin → HERMES_HOME/bin
NOVA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS_BIN="$NOVA_DIR/nova/agents/bin"
AGENTS_SCRIPTS="$NOVA_DIR/nova/agents/scripts"
AGENTS_SHELLS="$NOVA_DIR/nova/agents/shells"

echo "   NOVA install dir: $NOVA_DIR"
echo "   HERMES_HOME:      $HERMES_HOME"

# 에이전트 파일들 복사 (심볼릭 링크 대신 실제 복사 — 수정 가능하도록)
cp -f "$AGENTS_BIN"/nova_*.py     "$HERMES_HOME/bin/"     2>/dev/null && echo "   ✅ bin/ agents ($(ls "$AGENTS_BIN"/nova_*.py | wc -l)개)"
cp -f "$AGENTS_SCRIPTS"/nova_*.py "$HERMES_HOME/scripts/" 2>/dev/null && echo "   ✅ scripts/ agents ($(ls "$AGENTS_SCRIPTS"/nova_*.py | wc -l)개)"
cp -f "$AGENTS_SHELLS"/*.sh       "$HERMES_HOME/scripts/" 2>/dev/null && echo "   ✅ shell scripts ($(ls "$AGENTS_SHELLS"/*.sh | wc -l)개)"
chmod +x "$HERMES_HOME/scripts/"/*.sh 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Python 의존성 설치
# ---------------------------------------------------------------------------
echo ""
echo "▶ Installing Python dependencies..."
pip3 install -q -r "$NOVA_DIR/requirements.txt"
pip3 install -q -e "$NOVA_DIR"
echo "   ✅ nova package installed"

# ---------------------------------------------------------------------------
# 3. nova_brain.db 초기화 (SQLite)
# ---------------------------------------------------------------------------
echo ""
echo "▶ Initializing nova_brain.db..."
NOVA_BRAIN_DB="$HERMES_HOME/nova_brain.db"
export HERMES_HOME

if [ ! -f "$NOVA_BRAIN_DB" ]; then
  $PYTHON "$HERMES_HOME/bin/nova_brain_schema.py" init 2>/dev/null || \
  $PYTHON - <<'PYEOF'
import os, sqlite3
from pathlib import Path
home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
db = home / "nova_brain.db"
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL,
  title TEXT,
  content TEXT,
  category TEXT DEFAULT 'general',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  weight REAL DEFAULT 0.86,
  auto_link INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS takes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  take TEXT NOT NULL,
  context TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  importance TEXT DEFAULT 'MEDIUM',
  category TEXT DEFAULT 'general'
);
CREATE TABLE IF NOT EXISTS hermes_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload TEXT,
  source TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  processed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nova_learn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,
  content TEXT,
  quality_score REAL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()
conn.close()
print("   nova_brain.db initialized")
PYEOF
  echo "   ✅ nova_brain.db created at $NOVA_BRAIN_DB"
else
  echo "   ✅ nova_brain.db already exists — skipped"
fi

# ---------------------------------------------------------------------------
# 4. KB 기본 구조 생성
# ---------------------------------------------------------------------------
echo ""
echo "▶ Setting up KB structure..."
KB="$HERMES_HOME/kb"
mkdir -p "$KB"/{config,agents,projects,nova,audit_loop}
cat > "$KB/INDEX.md" <<'KBEOF'
# NOVA KB Index
Auto-generated by nova setup. Edit to add your own pages.
KBEOF
echo "   ✅ KB directories created"

# ---------------------------------------------------------------------------
# 5. .env 템플릿 생성
# ---------------------------------------------------------------------------
echo ""
echo "▶ Generating .env template..."
ENV_FILE="$HERMES_HOME/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# NOVA Environment Variables
# Fill in your values and run: source ~/.hermes/.env

# Hermes / LLM Provider
HERMES_API_KEY=your_api_key_here
HERMES_BASE_URL=https://api.openai.com/v1
HERMES_MODEL=gpt-4o

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional: Codex Gate (OpenAI Codex CLI integration)
OPENAI_API_KEY=

# HERMES_HOME (override if different from ~/.hermes)
# HERMES_HOME=/custom/path
ENVEOF
  echo "   ✅ .env template → $ENV_FILE"
  echo "   ⚠️  Fill in your API keys before running NOVA"
else
  echo "   ✅ .env already exists — skipped"
fi

# ---------------------------------------------------------------------------
# 6. nova-setup 초기화 파일 생성
# ---------------------------------------------------------------------------
cat > "$HERMES_HOME/nova_setup_complete" <<SETUPEOF
NOVA_VERSION=$NOVA_VERSION
INSTALLED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
HERMES_HOME=$HERMES_HOME
SETUPEOF

# ---------------------------------------------------------------------------
# 7. 완료 메시지
# ---------------------------------------------------------------------------
echo ""
echo "╔════════════════════════════════════════╗"
echo "║         NOVA Setup Complete! 🚀        ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Fill in your API keys:"
echo "     nano $HERMES_HOME/.env"
echo ""
echo "  2. Export HERMES_HOME:"
echo "     export HERMES_HOME=$HERMES_HOME"
echo ""
echo "  3. Start the brain watcher (event-driven core):"
echo "     python3 $HERMES_HOME/scripts/nova_brain_watcher.py &"
echo ""
echo "  4. Run the NOVA briefing to check system health:"
echo "     python3 $HERMES_HOME/scripts/nova_hermes_briefing.py"
echo ""
echo "  5. Start autonomous engine:"
echo "     python3 $HERMES_HOME/scripts/nova_autonomous_engine.py"
echo ""
echo "  Full documentation:"
echo "  → https://github.com/noivan0/NOVA/blob/main/docs/guides/quickstart.md"
echo ""
