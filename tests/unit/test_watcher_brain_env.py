"""tests/unit/test_watcher_brain_env.py — regression tests for the
`_apply_master_key_llm_defaults()` helper in nova/watcher/brain.py.

P1 fix (2026-08-18, Codex-audited round 2): removing the hardcoded private
HMG gateway URL default from NOVA_LLM_BASE_URL uncovered a real regression —
this helper used to unconditionally select NOVA_LLM_PROVIDER=hmg whenever a
Hermes master API key was found, but HMGProvider now raises ValueError when
base_url is empty. That combination made the direct brain-watcher crash on
startup in any master-key-only environment with no NOVA_LLM_BASE_URL set.
Codex cold audit reproduced this independently; see
kb/fixes/nova-oss-hmg-default-removal-watcher-regression-20260818.md.

P1 fix round 3 (same day, Codex re-audit): the round-2 fix only checked
os.environ for NOVA_LLM_BASE_URL, so a user who configured base_url in
nova.yaml (not as an env var) got silently overridden to the 'echo'
provider — os.environ["NOVA_LLM_PROVIDER"]="echo" then took priority over
the YAML's "provider: hmg" once load_config()'s env-override step ran.
Codex reproduced this with an actual load_config() round-trip. Fixed by
having the helper also check nova.yaml's llm.base_url when a path is given.
"""
import os
import tempfile
from pathlib import Path

import pytest

from nova.core.config import load_config
from nova.watcher.brain import _apply_master_key_llm_defaults


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    """Ensure each test starts from a clean LLM-related env slate."""
    for var in (
        "NOVA_LLM_PROVIDER", "NOVA_LLM_MODEL", "NOVA_LLM_BASE_URL",
        "NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY",
        "NOVA_CODEX_API_KEY", "NOVA_IMAGE_GEN_API_KEY",
        "HERMES_MASTER_APIKEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_falls_back_to_echo_when_no_base_url_configured():
    """Regression test: no NOVA_LLM_BASE_URL set -> must select 'echo', not
    'hmg' (selecting 'hmg' here is exactly what made HMGProvider raise
    ValueError on startup before this fix)."""
    _apply_master_key_llm_defaults("some-master-key")
    assert os.environ["NOVA_LLM_PROVIDER"] == "echo"
    assert "NOVA_LLM_MODEL" not in os.environ


def test_selects_hmg_only_when_base_url_already_set(monkeypatch):
    """When the user has already configured an explicit base URL, 'hmg' is
    a safe, working default — this is the one case selecting it is fine."""
    monkeypatch.setenv("NOVA_LLM_BASE_URL", "https://my-own-gateway.example.com/v2")
    _apply_master_key_llm_defaults("some-master-key")
    assert os.environ["NOVA_LLM_PROVIDER"] == "hmg"
    assert os.environ["NOVA_LLM_MODEL"] == "claude-sonnet-4-6"


def test_does_not_override_explicit_user_provider(monkeypatch):
    """A user-configured provider must never be silently overridden."""
    monkeypatch.setenv("NOVA_LLM_PROVIDER", "openai")
    _apply_master_key_llm_defaults("some-master-key")
    assert os.environ["NOVA_LLM_PROVIDER"] == "openai"


def test_injects_master_key_into_all_known_api_key_vars():
    _apply_master_key_llm_defaults("secret-123")
    for var in (
        "NOVA_LLM_API_KEY", "HMG_API_KEY", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "NOVA_KB_EMBEDDING_API_KEY",
        "NOVA_CODEX_API_KEY", "NOVA_IMAGE_GEN_API_KEY",
        "HERMES_MASTER_APIKEY",
    ):
        assert os.environ[var] == "secret-123"


def test_yaml_only_base_url_does_not_get_overridden_to_echo():
    """Regression test (Codex round-3 audit): base_url set ONLY in
    nova.yaml (not as an env var) must still select 'hmg', and that choice
    must survive a real load_config() round-trip — not get silently
    overridden to 'echo' by this helper's own env write.
    """
    with tempfile.TemporaryDirectory() as tmp:
        nova_yaml = Path(tmp) / "nova.yaml"
        nova_yaml.write_text(
            "llm:\n"
            "  provider: hmg\n"
            "  model: claude-sonnet-4-6\n"
            "  api_key: configured-key\n"
            "  base_url: https://configured.example.invalid/v2\n"
        )
        _apply_master_key_llm_defaults("master-key", nova_yaml_path=nova_yaml)
        assert os.environ["NOVA_LLM_PROVIDER"] == "hmg"

        cfg = load_config(str(nova_yaml))
        assert cfg.llm.provider == "hmg", (
            "YAML-configured 'hmg' provider was overridden by this helper's "
            "own env default — env-only base_url check is insufficient"
        )
        assert cfg.llm.base_url == "https://configured.example.invalid/v2"


def test_no_base_url_anywhere_falls_back_to_echo_even_with_yaml_path():
    """When nova.yaml exists but sets no base_url, and no env var sets one
    either, the helper must still fall back to 'echo' (not silently select
    'hmg', which would then crash with HMGProvider's ValueError)."""
    with tempfile.TemporaryDirectory() as tmp:
        nova_yaml = Path(tmp) / "nova.yaml"
        nova_yaml.write_text("llm:\n  provider: hmg\n")  # no base_url anywhere
        _apply_master_key_llm_defaults("master-key", nova_yaml_path=nova_yaml)
        assert os.environ["NOVA_LLM_PROVIDER"] == "echo"


def test_missing_yaml_path_does_not_crash():
    """A nova_yaml_path that doesn't exist must be handled gracefully
    (falls back to echo, same as no path given)."""
    missing = Path(tempfile.mkdtemp()) / "does_not_exist.yaml"
    _apply_master_key_llm_defaults("master-key", nova_yaml_path=missing)
    assert os.environ["NOVA_LLM_PROVIDER"] == "echo"
