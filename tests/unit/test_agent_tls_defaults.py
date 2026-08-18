"""tests/unit/test_agent_tls_defaults.py — regression tests ensuring the
legacy nova/agents/ scripts default to real TLS verification and only
disable it via an explicit NOVA_DISABLE_SSL_VERIFY=1/true/yes/on opt-out.

P1 fix (2026-08-18): these scripts (nova/agents/bin/, nova/agents/scripts/)
previously disabled TLS certificate/hostname verification unconditionally
in several places — appropriate only for the original author's internal
network with a self-signed gateway cert, dangerous for every other user.
Codex found two additional violations in a full-tree sweep after the first
fix pass (session 20260818_121534_823fb8):
  - nova/agents/scripts/nova_brain_watchdog.py's Telegram alert sender
    unconditionally used ssl.CERT_NONE, exposing the bot token embedded in
    the request URL to a network MITM.
  - nova/agents/scripts/nova_codex_gate.py's GPT L2 call used a local
    `not bool(os.environ.get(...))` predicate that treated ANY non-empty
    string ("0", "false", "no", "off") as "disable verification", diverging
    from the correct module-level SSL_VERIFY allowlist in the same file.

These tests import the actual modules and check the resulting SSLContext /
verify value under: (a) no env var set, (b) NOVA_DISABLE_SSL_VERIFY=1, and
(c) NOVA_DISABLE_SSL_VERIFY=false/0 (must NOT disable verification).
"""
import importlib
import importlib.util
import os
import ssl
import sys
from pathlib import Path

import pytest

_AGENTS_BIN = Path(__file__).resolve().parent.parent.parent / "nova" / "agents" / "bin"
_AGENTS_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "nova" / "agents" / "scripts"


@pytest.fixture(autouse=True)
def _clean_ssl_env(monkeypatch):
    monkeypatch.delenv("NOVA_DISABLE_SSL_VERIFY", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("NOVA_FORCE_SSL_VERIFY", raising=False)
    yield


def _load_module_fresh(path: Path, name: str):
    """Load a standalone script module fresh (these aren't real packages)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_nova_llm_ssl_ctx_verifies_by_default():
    mod = _load_module_fresh(_AGENTS_BIN / "nova_llm.py", "_test_nova_llm_bin")
    ctx = mod._ssl_ctx()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_nova_llm_ssl_ctx_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("NOVA_DISABLE_SSL_VERIFY", "1")
    mod = _load_module_fresh(_AGENTS_BIN / "nova_llm.py", "_test_nova_llm_bin_optout")
    ctx = mod._ssl_ctx()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("falsy_value", ["0", "false", "no", "off", ""])
def test_nova_llm_ssl_ctx_does_not_disable_on_falsy_strings(monkeypatch, falsy_value):
    """Regression: a naive `not bool(env_var)` check would treat "0"/"false"
    as falsy-but-nonempty and NOT disable verification by accident in the
    wrong direction, or (the actual bug found) treat any nonempty string as
    "disable". Confirm the allowlist only matches the real truthy tokens."""
    monkeypatch.setenv("NOVA_DISABLE_SSL_VERIFY", falsy_value)
    mod = _load_module_fresh(_AGENTS_BIN / "nova_llm.py", f"_test_nova_llm_bin_{falsy_value or 'empty'}")
    ctx = mod._ssl_ctx()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_resource_collector_ssl_verify_defaults_true():
    mod = _load_module_fresh(
        _AGENTS_SCRIPTS / "nova_resource_collector.py", "_test_resource_collector"
    )
    assert mod.SSL_VERIFY is True


def test_resource_collector_ssl_verify_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("NOVA_DISABLE_SSL_VERIFY", "true")
    mod = _load_module_fresh(
        _AGENTS_SCRIPTS / "nova_resource_collector.py", "_test_resource_collector_optout"
    )
    assert mod.SSL_VERIFY is False
    ctx = mod._ssl_ctx()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("falsy_value", ["0", "false", "no", "off"])
def test_resource_collector_old_env_var_name_no_longer_disables(monkeypatch, falsy_value):
    """Regression: the old NOVA_FORCE_SSL_VERIFY env var must no longer have
    any effect — only NOVA_DISABLE_SSL_VERIFY does."""
    monkeypatch.setenv("NOVA_FORCE_SSL_VERIFY", falsy_value)
    mod = _load_module_fresh(
        _AGENTS_SCRIPTS / "nova_resource_collector.py", f"_test_resource_collector_legacy_{falsy_value}"
    )
    assert mod.SSL_VERIFY is True


def test_codex_gate_module_level_ssl_verify_defaults_true():
    mod = _load_module_fresh(_AGENTS_SCRIPTS / "nova_codex_gate.py", "_test_codex_gate")
    assert mod.SSL_VERIFY is True


@pytest.mark.parametrize("falsy_value", ["0", "false", "no", "off"])
def test_codex_gate_module_level_ssl_verify_not_disabled_by_falsy_strings(monkeypatch, falsy_value):
    monkeypatch.setenv("NOVA_DISABLE_SSL_VERIFY", falsy_value)
    mod = _load_module_fresh(
        _AGENTS_SCRIPTS / "nova_codex_gate.py", f"_test_codex_gate_{falsy_value}"
    )
    assert mod.SSL_VERIFY is True


def test_codex_gate_gpt_audit_uses_module_level_ssl_verify_not_local_predicate():
    """Regression: gpt_audit() used to compute its own
    `not bool(os.environ.get("NOVA_DISABLE_SSL_VERIFY", ""))` predicate,
    disabling verification for ANY nonempty value including "0"/"false".
    Confirm the source no longer contains that pattern and instead reuses
    the module-level SSL_VERIFY."""
    src = (_AGENTS_SCRIPTS / "nova_codex_gate.py").read_text()
    assert "ssl_verify = not bool(" not in src, (
        "the inconsistent local ssl_verify predicate must not reappear in gpt_audit()"
    )


def test_brain_watchdog_send_alert_verifies_by_default(monkeypatch):
    """Regression: send_alert() used to unconditionally build a CERT_NONE
    context (the Telegram bot token is embedded in the request URL, so an
    unverified TLS connection exposes it to a network MITM). Verify by
    actually calling send_alert() with urllib.request.urlopen mocked out
    and inspecting the ssl.SSLContext it was given — not by string-matching
    the source, which is brittle to formatting changes.
    """
    mod = _load_module_fresh(_AGENTS_SCRIPTS / "nova_brain_watchdog.py", "_test_brain_watchdog")

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=15, context=None):
        captured["context"] = context
        return _FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    mod.send_alert("fake-token", "test message")

    ctx = captured["context"]
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_brain_watchdog_send_alert_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("NOVA_DISABLE_SSL_VERIFY", "1")
    mod = _load_module_fresh(_AGENTS_SCRIPTS / "nova_brain_watchdog.py", "_test_brain_watchdog_optout")

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=15, context=None):
        captured["context"] = context
        return _FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    mod.send_alert("fake-token", "test message")

    ctx = captured["context"]
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE
