"""tests/unit/test_config.py — Unit tests for config loading."""
import os
import tempfile

from nova.core.config import NOVAConfig, load_config

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
    cases = [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
    ]
    for val, expected in cases:
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


def test_careful_defaults_are_safe_by_default():
    """careful_enabled/allow_medium_override는 기본값이 안전 쪽(둘 다 True)
    이어야 한다 — 기존 사용자가 아무 것도 안 바꿔도 위험명령 탐지가 켜져
    있어야 gstack `/careful` parity 도입이 opt-in이 아닌 안전기본값이 된다."""
    cfg = NOVAConfig()
    assert cfg.careful_enabled is True
    assert cfg.careful_allow_medium_override is True


def test_careful_enabled_env_override():
    old = os.environ.get("NOVA_CAREFUL_ENABLED")
    os.environ["NOVA_CAREFUL_ENABLED"] = "false"
    try:
        cfg = load_config("/nonexistent/nova.yaml")
        assert cfg.careful_enabled is False
    finally:
        if old is None:
            os.environ.pop("NOVA_CAREFUL_ENABLED", None)
        else:
            os.environ["NOVA_CAREFUL_ENABLED"] = old


def test_careful_allow_medium_override_env_override():
    old = os.environ.get("NOVA_CAREFUL_ALLOW_MEDIUM_OVERRIDE")
    os.environ["NOVA_CAREFUL_ALLOW_MEDIUM_OVERRIDE"] = "false"
    try:
        cfg = load_config("/nonexistent/nova.yaml")
        assert cfg.careful_allow_medium_override is False
    finally:
        if old is None:
            os.environ.pop("NOVA_CAREFUL_ALLOW_MEDIUM_OVERRIDE", None)
        else:
            os.environ["NOVA_CAREFUL_ALLOW_MEDIUM_OVERRIDE"] = old


def test_careful_settings_from_yaml():
    yaml_content = "careful_enabled: false\ncareful_allow_medium_override: false\n"
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml_content)
        cfg = load_config(path)
        assert cfg.careful_enabled is False
        assert cfg.careful_allow_medium_override is False
    finally:
        os.unlink(path)
