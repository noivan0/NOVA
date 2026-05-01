"""tests/unit/test_config.py — Unit tests for config loading."""
import os
import tempfile
from pathlib import Path

from nova.core.config import load_config, NOVAConfig


SAMPLE_YAML = """\
workspace: ./my-workspace
phase_timeout: 600
max_retries: 3
quality_threshold: 80

llm:
  provider: anthropic
  model: claude-haiku-4-5
  max_tokens: 2048
  temperature: 0.5

notifier:
  provider: telegram
  token: test-token
  chat_id: "-1001234567890"

publisher:
  provider: file
  output_dir: ./out

kb:
  path: ./my-kb
  auto_record: false
"""


def test_load_defaults():
    """With no file and no env, defaults should be returned."""
    cfg = load_config("/nonexistent/path/nova.yaml")
    assert isinstance(cfg, NOVAConfig)
    assert cfg.llm.provider == "openai"
    assert cfg.notifier.provider == "none"
    assert cfg.publisher.provider == "none"
    assert cfg.dry_run is False


def test_load_from_yaml():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        path = f.name
    try:
        cfg = load_config(path)
        assert cfg.workspace == "./my-workspace"
        assert cfg.phase_timeout == 600
        assert cfg.max_retries == 3
        assert cfg.quality_threshold == 80
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model == "claude-haiku-4-5"
        assert cfg.llm.max_tokens == 2048
        assert cfg.llm.temperature == 0.5
        assert cfg.notifier.provider == "telegram"
        assert cfg.notifier.token == "test-token"
        assert cfg.publisher.provider == "file"
        assert cfg.kb.path == "./my-kb"
        assert cfg.kb.auto_record is False
    finally:
        os.unlink(path)


def test_env_overrides_yaml():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        path = f.name
    try:
        env_patch = {
            "NOVA_LLM_PROVIDER": "openai",
            "NOVA_LLM_MODEL": "gpt-4o",
            "NOVA_DRY_RUN": "true",
            "NOVA_QUALITY_THRESHOLD": "55",
            "NOVA_LLM_TIMEOUT": "300",
        }
        old = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        try:
            cfg = load_config(path)
            assert cfg.llm.provider == "openai"
            assert cfg.llm.model == "gpt-4o"
            assert cfg.dry_run is True
            assert cfg.quality_threshold == 55
            assert cfg.llm.timeout == 300
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        os.unlink(path)


def test_dry_run_env_variants():
    for val, expected in [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)]:
        old = os.environ.get("NOVA_DRY_RUN")
        os.environ["NOVA_DRY_RUN"] = val
        try:
            cfg = load_config("/nonexistent/nova.yaml")
            assert cfg.dry_run is expected, f"NOVA_DRY_RUN={val} should be {expected}"
        finally:
            if old is None:
                os.environ.pop("NOVA_DRY_RUN", None)
            else:
                os.environ["NOVA_DRY_RUN"] = old
