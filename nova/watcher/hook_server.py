"""
nova.watcher.hook_server — Published Hook Server: lightweight HTTP webhook receiver.

Receives publish-complete events from external agents/publishers and immediately
triggers downstream sync — without waiting for a cron job.

Usage::

    python -m nova.watcher.hook_server --nova-home ~/.nova --port 9121

API
---
POST /publish
    Body: {"blog": "myblog", "category": "tech", "title": "...", "url": "...", "quality_score": 85}
    Response: {"ok": true, "blog": "myblog", "title": "..."}
    Side effects:
      - Adds to Redis timeline (if redis is available)
      - Triggers sync_published (10-min cooldown)
      - Triggers geo_update if data file changed (6-h cooldown)

GET /health
    Response: {"status": "ok", "ts": "..."}

GET /check?blog=<blog>&category=<cat>&title=<title>
    Response: {"duplicate": false, "blog": "...", "category": "..."}

Redis is optional. If unavailable, the server still functions but does not
persist timeline data between restarts.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hook-server] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ── state (module-level, shared across requests in one process) ───────────────

_last_sync_time: float = 0
_last_geo_time: float = 0
_geo_mtime_at_last_run: float = 0
_lock = threading.Lock()

SYNC_COOLDOWN_S: float = 600       # 10 minutes
GEO_COOLDOWN_S: float  = 6 * 3600  # 6 hours


# ── Redis (optional) ──────────────────────────────────────────────────────────

def _try_redis():
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, db=0, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


_redis = _try_redis()


def _redis_record(blog: str, category: str, title: str, url: str, quality_score: int) -> None:
    if not _redis:
        return
    try:
        key = f"published:{blog}:{category}"
        _redis.sadd(key, title)
        timeline_key = f"timeline:{blog}"
        entry = json.dumps({
            "title": title, "category": category, "url": url,
            "quality_score": quality_score,
            "ts": datetime.utcnow().isoformat(),
        }, ensure_ascii=False)
        _redis.lpush(timeline_key, entry)
        _redis.ltrim(timeline_key, 0, 499)
    except Exception as e:
        logger.warning(f"redis error: {e}")


def _redis_check_dup(blog: str, category: str, title: str) -> bool:
    if not _redis:
        return False
    try:
        key = f"published:{blog}:{category}"
        members = list(_redis.sscan_iter(key))
        return title in members
    except Exception:
        return False


# ── downstream triggers ───────────────────────────────────────────────────────

def _trigger_sync(sync_script: Path | None, blog: str, title: str) -> None:
    global _last_sync_time
    with _lock:
        now = time.time()
        if now - _last_sync_time < SYNC_COOLDOWN_S:
            remaining = int(SYNC_COOLDOWN_S - (now - _last_sync_time))
            logger.info(f"[SYNC] cooldown active ({remaining}s remaining) — skip")
            return
        _last_sync_time = now

    def _worker() -> None:
        if not sync_script or not sync_script.exists():
            return
        try:
            r = subprocess.run(
                [sys.executable, str(sync_script)],
                capture_output=True, text=True, timeout=120,
            )
            lines = (r.stdout or "").strip().splitlines()
            tail = lines[-1][:120] if lines else "ok"
            logger.info(f"[SYNC] done: {tail}")
        except Exception as e:
            logger.error(f"[SYNC] error: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    logger.info(f"[SYNC] triggered → sync_published [{blog}] {title}")


def _trigger_geo(geo_script: Path | None, geo_data: Path | None) -> None:
    global _last_geo_time, _geo_mtime_at_last_run
    if not geo_script or not geo_data:
        return
    with _lock:
        now = time.time()
        if now - _last_geo_time < GEO_COOLDOWN_S:
            return
        try:
            cur_mtime = geo_data.stat().st_mtime
        except Exception:
            return
        if cur_mtime == _geo_mtime_at_last_run:
            return  # data unchanged since last run
        _last_geo_time = now
        _geo_mtime_at_last_run = cur_mtime

    def _worker() -> None:
        try:
            r = subprocess.run(
                [sys.executable, str(geo_script)],
                capture_output=True, text=True, timeout=300,
            )
            lines = (r.stdout or "").strip().splitlines()
            tail = lines[-1][:120] if lines else "ok"
            logger.info(f"[GEO] done: {tail}")
        except Exception as e:
            logger.error(f"[GEO] error: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    logger.info("[GEO] data changed → geo_update triggered")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def __init__(self, *args, sync_script, geo_script, geo_data, **kwargs):
        self._sync_script = sync_script
        self._geo_script = geo_script
        self._geo_data = geo_data
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: A002
        logger.info(format % args)

    def _token_ok(self) -> bool:
        """Return True if the configured shared-secret token (if any)
        matches the request's X-Nova-Hook-Token header.

        SECURITY-012 (2026-08-18, Codex-audited round 4): the initial
        SECURITY-010 fix only gated POST /publish. Codex pointed out that
        GET /check discloses whether a given (blog, category, title)
        combination has already been published -- a real information
        disclosure surface (blog/category names can be confidential
        project codenames) -- and this endpoint remained completely
        unauthenticated even in the exact "NOVA_HOOK_TOKEN set + bind
        0.0.0.0" deployment the SECURITY-010 fix's own docstring
        recommends for legitimate remote exposure. Factored the check out
        so both /publish (POST) and /check (GET) enforce the same gate.
        """
        expected_token = os.environ.get("NOVA_HOOK_TOKEN", "")
        if not expected_token:
            return True  # no token configured -- rely on the 127.0.0.1-only bind
        provided = self.headers.get("X-Nova-Hook-Token", "")
        return hmac.compare_digest(provided, expected_token)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "ts": datetime.utcnow().isoformat()})
        elif self.path.startswith("/check"):
            if not self._token_ok():
                self._json(401, {"error": "unauthorized"})
                return
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            blog = qs.get("blog", [""])[0]
            category = qs.get("category", [""])[0]
            title = qs.get("title", [""])[0][:80]
            if blog and title:
                is_dup = _redis_check_dup(blog, category, title)
                self._json(200, {"duplicate": is_dup, "blog": blog, "category": category})
            else:
                self._json(400, {"error": "blog and title required"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/publish":
            self._json(404, {"error": "not found"})
            return

        # SECURITY-010 (2026-08-18, deep audit round 4): this endpoint
        # had NO authentication at all. `nova watcher start` runs this
        # server by default (opt-out via --no-hook), bound to 0.0.0.0,
        # so on any host without a strict firewall an anonymous remote
        # client could POST arbitrary data here — triggering
        # sync_published.py/geo_update.py subprocess runs on demand
        # (resource-exhaustion DoS) and injecting arbitrary rows into
        # Redis's published-set/timeline (data poisoning). Reproduced:
        # an unauthenticated POST /publish succeeded and fired the sync
        # trigger. Require a shared-secret token (NOVA_HOOK_TOKEN) via
        # the X-Nova-Hook-Token header, checked with hmac.compare_digest
        # to avoid timing side-channels, whenever a token is configured.
        if not self._token_ok():
            self._json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        blog = data.get("blog", "")
        category = data.get("category", "")
        title = data.get("title", "")[:80]
        url = data.get("url", "")
        quality_score = int(data.get("quality_score", 0))

        if not blog or not title:
            self._json(400, {"error": "blog and title required"})
            return

        _redis_record(blog, category, title, url, quality_score)
        logger.info(f"✅ published: [{blog}/{category}] {title}")

        # Downstream triggers (non-blocking)
        _trigger_sync(self._sync_script, blog, title)
        _trigger_geo(self._geo_script, self._geo_data)

        self._json(200, {"ok": True, "blog": blog, "title": title})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── server factory ────────────────────────────────────────────────────────────

def make_server(
    port: int = 9121,
    nova_home: Path | None = None,
    sync_script: Path | None = None,
    geo_script: Path | None = None,
    geo_data: Path | None = None,
    host: str | None = None,
) -> HTTPServer:
    """Build and return the HTTPServer (does not start it).

    SECURITY-010 (2026-08-18, deep audit round 4): defaults to binding
    127.0.0.1 instead of 0.0.0.0 unless a shared-secret token
    (NOVA_HOOK_TOKEN) is configured, so a fresh `nova watcher start` on a
    host without a strict firewall doesn't expose an unauthenticated
    webhook endpoint to the whole network by default. Set NOVA_HOOK_TOKEN
    and pass host="0.0.0.0" explicitly to intentionally expose it beyond
    localhost (e.g. behind a reverse proxy).
    """
    if nova_home:
        engines_dir = nova_home / "engines"
        if sync_script is None:
            p = engines_dir / "sync_published.py"
            sync_script = p if p.exists() else None
        if geo_script is None:
            p = engines_dir / "geo_update.py"
            geo_script = p if p.exists() else None
        if geo_data is None:
            p = nova_home / "data" / "geo_experiments.jsonl"
            geo_data = p if p.exists() else None

    def handler_factory(*args, **kwargs):
        return _Handler(
            *args,
            sync_script=sync_script,
            geo_script=geo_script,
            geo_data=geo_data,
            **kwargs,
        )

    if host is None:
        host = "0.0.0.0" if os.environ.get("NOVA_HOOK_TOKEN") else "127.0.0.1"
        if host == "127.0.0.1":
            logger.warning(
                "NOVA_HOOK_TOKEN not set — binding 127.0.0.1 only (localhost). "
                "Set NOVA_HOOK_TOKEN and pass host='0.0.0.0' to expose this "
                "endpoint beyond localhost; it has no other authentication."
            )
    return HTTPServer((host, port), handler_factory)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NOVA Hook Server — webhook receiver for publish-complete events"
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("NOVA_HOOK_PORT", "9121")))
    parser.add_argument(
        "--host", default=None,
        help="Bind address. Default: 127.0.0.1 unless NOVA_HOOK_TOKEN is set "
             "(then 0.0.0.0). Pass explicitly to override either way.",
    )
    parser.add_argument(
        "--nova-home", default=os.environ.get("NOVA_HOME", "~/.nova"),
        help="NOVA data directory",
    )
    args = parser.parse_args()

    nova_home = Path(args.nova_home).expanduser().resolve()
    server = make_server(port=args.port, nova_home=nova_home, host=args.host)
    bound_host = server.server_address[0]
    logger.info(f"hook-server listening on {bound_host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("hook-server stopped")


if __name__ == "__main__":
    main()
