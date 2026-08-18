"""tests/unit/test_hook_server_auth.py — regression tests for
nova/watcher/hook_server.py's missing authentication.

SECURITY-010 (2026-08-18, deep audit round 4): the /publish endpoint had
NO authentication at all. `nova watcher start` runs this server by
default (opt-out via --no-hook), bound to 0.0.0.0, so on any host
without a strict firewall an anonymous remote client could POST
arbitrary data -- triggering sync_published.py/geo_update.py subprocess
runs on demand (resource-exhaustion DoS) and injecting arbitrary rows
into Redis (data poisoning). Reproduced: an unauthenticated POST
/publish succeeded and fired the sync trigger.

Fixed two ways:
1. A shared-secret token (NOVA_HOOK_TOKEN) is required via the
   X-Nova-Hook-Token header (checked with hmac.compare_digest) whenever
   the token is configured.
2. The default bind address is 127.0.0.1 (not 0.0.0.0) unless
   NOVA_HOOK_TOKEN is set, so a fresh install without a configured token
   isn't reachable from the network at all by default.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from nova.watcher.hook_server import make_server


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv("NOVA_HOOK_TOKEN", raising=False)
    yield


def _post(base_url: str, payload: dict, token: str | None = None):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Nova-Hook-Token"] = token
    req = urllib.request.Request(
        f"{base_url}/publish", data=data, headers=headers, method="POST"
    )
    return urllib.request.urlopen(req, timeout=5)


def test_default_bind_address_is_localhost_only_without_token():
    """Regression test: without NOVA_HOOK_TOKEN configured, the server
    must not bind 0.0.0.0 by default (that would expose an
    unauthenticated endpoint to the whole network)."""
    server = make_server(port=0, nova_home=None)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_bind_address_becomes_0000_when_token_configured(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "some-secret")
    server = make_server(port=0, nova_home=None)
    try:
        assert server.server_address[0] == "0.0.0.0"
    finally:
        server.server_close()


def test_explicit_host_overrides_the_default(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "some-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_publish_without_token_configured_still_works_locally():
    """Backwards compatibility: if the operator hasn't configured
    NOVA_HOOK_TOKEN at all, /publish still accepts requests (there's no
    secret to check against) -- the safety net is the 127.0.0.1-only
    bind address in that case, not blocking every request."""
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        resp = _post(f"http://127.0.0.1:{port}", {"blog": "b", "category": "c", "title": "t"})
        assert resp.status == 200
    finally:
        server.shutdown()


def test_publish_with_token_configured_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(f"http://127.0.0.1:{port}", {"blog": "b", "category": "c", "title": "t"})
        assert exc_info.value.code == 401
    finally:
        server.shutdown()


def test_publish_with_token_configured_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(
                f"http://127.0.0.1:{port}",
                {"blog": "b", "category": "c", "title": "t"},
                token="wrong-secret",
            )
        assert exc_info.value.code == 401
    finally:
        server.shutdown()


def test_publish_with_correct_token_succeeds(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        resp = _post(
            f"http://127.0.0.1:{port}",
            {"blog": "b", "category": "c", "title": "t"},
            token="correct-secret",
        )
        assert resp.status == 200
    finally:
        server.shutdown()


def _get(url: str, token: str | None = None):
    headers = {}
    if token is not None:
        headers["X-Nova-Hook-Token"] = token
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=5)


def test_check_endpoint_without_token_configured_still_works_locally():
    """Backwards compatibility: no NOVA_HOOK_TOKEN configured -> /check
    still answers (the safety net is the 127.0.0.1-only bind)."""
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        resp = _get(f"http://127.0.0.1:{port}/check?blog=b&category=c&title=t")
        assert resp.status == 200
    finally:
        server.shutdown()


def test_check_endpoint_with_token_configured_rejects_missing_token(monkeypatch):
    """SECURITY-012 (2026-08-18, Codex-audited round 4): GET /check must
    require the same token as POST /publish once NOVA_HOOK_TOKEN is
    configured -- otherwise the exact 'token set + bind 0.0.0.0' deployment
    the SECURITY-010 fix recommends for legitimate remote exposure would
    still leak duplicate-check results (blog/category names can be
    confidential) to any anonymous network client."""
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"http://127.0.0.1:{port}/check?blog=secret-project&category=c&title=t")
        assert exc_info.value.code == 401
    finally:
        server.shutdown()


def test_check_endpoint_with_correct_token_succeeds(monkeypatch):
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        resp = _get(
            f"http://127.0.0.1:{port}/check?blog=b&category=c&title=t",
            token="correct-secret",
        )
        assert resp.status == 200
    finally:
        server.shutdown()


def test_health_endpoint_remains_unauthenticated_even_with_token(monkeypatch):
    """/health intentionally stays open (low-sensitivity liveness check)
    even when a token is configured -- regression guard against
    accidentally over-gating it."""
    monkeypatch.setenv("NOVA_HOOK_TOKEN", "correct-secret")
    server = make_server(port=0, nova_home=None, host="127.0.0.1")
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        resp = _get(f"http://127.0.0.1:{port}/health")
        assert resp.status == 200
    finally:
        server.shutdown()
