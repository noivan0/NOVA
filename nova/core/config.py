"""
nova/core/config.py
-------------------
Loads NOVA configuration from environment variables and nova.yaml.
All provider credentials, paths, and behaviour flags are read here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class LLMConfig:
    provider: str = "openai"          # openai | anthropic | ollama | custom
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout: int = 120


@dataclass
class NotifierConfig:
    provider: str = "none"            # none | telegram | slack | discord | webhook
    token: str = ""
    chat_id: str = ""
    webhook_url: str = ""


@dataclass
class PublisherConfig:
    provider: str = "none"            # none | wordpress | ghost | file
    api_key: str = ""
    base_url: str = ""
    output_dir: str = "./output"


@dataclass
class CodexConfig:
    """HMG Codex (GPT-5.4) 보조 LLM 설정"""
    provider: str = "openai"
    model: str = "gpt-5.4"
    api_key: str = ""
    base_url: Optional[str] = "https://h-chat-api.autoever.com/codex-cli/v2"
    max_tokens: int = 4096
    temperature: float = 0.7


@dataclass
class ImageGenConfig:
    """HMG Gemini 이미지 생성 설정"""
    provider: str = "hmg_gemini"
    model: str = "gemini-3.1-flash-image-preview"
    fallback_model: str = "gemini-3-pro-image-preview"
    api_key: str = ""   # 마스터 키와 동일; 비어있으면 NOVA_LLM_API_KEY 사용
    base_url: str = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3/models"


@dataclass
class KBConfig:
    path: str = "./kb"
    auto_record: bool = True
    embedding_enabled: bool = False
    embedding_provider: str = "hmg_openai"
    embedding_model: str = "text-embedding-3-large"
    embedding_base_url: str = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3/openai/deployments"
    embedding_api_key: str = ""   # 마스터 키와 동일; 비어있으면 NOVA_LLM_API_KEY 사용


@dataclass
class NOVAConfig:
    # Paths
    workspace: str = "./workspace"
    harnesses_dir: str = "./harnesses"
    kb: KBConfig = field(default_factory=KBConfig)

    # Providers
    llm: LLMConfig = field(default_factory=LLMConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    publisher: PublisherConfig = field(default_factory=PublisherConfig)

    # Behaviour
    phase_timeout: int = 300          # seconds per phase
    max_retries: int = 2
    quality_threshold: int = 70       # min score to proceed (0-100)
    evolution_enabled: bool = True
    dry_run: bool = False


def load_config(config_path: str = "nova.yaml") -> NOVAConfig:
    """
    Load config from nova.yaml, then override with environment variables.

    Environment variable mapping (subset):
      NOVA_LLM_PROVIDER     -> llm.provider
      NOVA_LLM_MODEL        -> llm.model
      NOVA_LLM_API_KEY      -> llm.api_key
      NOVA_LLM_BASE_URL     -> llm.base_url
      NOVA_NOTIFIER_PROVIDER-> notifier.provider
      NOVA_NOTIFIER_TOKEN   -> notifier.token
      NOVA_NOTIFIER_CHAT_ID -> notifier.chat_id
      NOVA_PUBLISHER_PROVIDER-> publisher.provider
      NOVA_KB_PATH          -> kb.path
      NOVA_WORKSPACE        -> workspace
      NOVA_DRY_RUN          -> dry_run (true/false)
    """
    cfg = NOVAConfig()

    # 1. Load from YAML if it exists
    p = Path(config_path)
    if p.exists():
        with open(p) as f:
            raw = yaml.safe_load(f) or {}
        _apply_yaml(cfg, raw)

    # 2. Override with environment variables
    _apply_env(cfg)

    return cfg


def _apply_yaml(cfg: NOVAConfig, raw: dict) -> None:
    """Recursively apply parsed YAML onto the dataclass tree."""
    if "workspace" in raw:
        cfg.workspace = raw["workspace"]
    if "harnesses_dir" in raw:
        cfg.harnesses_dir = raw["harnesses_dir"]
    if "phase_timeout" in raw:
        cfg.phase_timeout = int(raw["phase_timeout"])
    if "max_retries" in raw:
        cfg.max_retries = int(raw["max_retries"])
    if "quality_threshold" in raw:
        cfg.quality_threshold = int(raw["quality_threshold"])
    if "evolution_enabled" in raw:
        cfg.evolution_enabled = bool(raw["evolution_enabled"])
    if "dry_run" in raw:
        cfg.dry_run = bool(raw["dry_run"])

    if "kb" in raw:
        kb = raw["kb"]
        cfg.kb.path = kb.get("path", cfg.kb.path)
        cfg.kb.auto_record = kb.get("auto_record", cfg.kb.auto_record)
        cfg.kb.embedding_enabled = kb.get("embedding_enabled", cfg.kb.embedding_enabled)
        cfg.kb.embedding_provider = kb.get("embedding_provider", cfg.kb.embedding_provider)
        cfg.kb.embedding_model = kb.get("embedding_model", cfg.kb.embedding_model)
        cfg.kb.embedding_base_url = kb.get("embedding_base_url", cfg.kb.embedding_base_url)
        cfg.kb.embedding_api_key = kb.get("embedding_api_key", cfg.kb.embedding_api_key)

    if "codex" in raw:
        cx = raw["codex"]
        cfg.codex.provider = cx.get("provider", cfg.codex.provider)
        cfg.codex.model = cx.get("model", cfg.codex.model)
        cfg.codex.api_key = cx.get("api_key", cfg.codex.api_key)
        cfg.codex.base_url = cx.get("base_url", cfg.codex.base_url)
        cfg.codex.max_tokens = int(cx.get("max_tokens", cfg.codex.max_tokens))
        cfg.codex.temperature = float(cx.get("temperature", cfg.codex.temperature))

    if "image_gen" in raw:
        ig = raw["image_gen"]
        cfg.image_gen.provider = ig.get("provider", cfg.image_gen.provider)
        cfg.image_gen.model = ig.get("model", cfg.image_gen.model)
        cfg.image_gen.fallback_model = ig.get("fallback_model", cfg.image_gen.fallback_model)
        cfg.image_gen.api_key = ig.get("api_key", cfg.image_gen.api_key)
        cfg.image_gen.base_url = ig.get("base_url", cfg.image_gen.base_url)

    if "llm" in raw:
        llm_cfg = raw["llm"]
        cfg.llm.provider = llm_cfg.get("provider", cfg.llm.provider)
        cfg.llm.model = llm_cfg.get("model", cfg.llm.model)
        cfg.llm.api_key = llm_cfg.get("api_key", cfg.llm.api_key)
        cfg.llm.base_url = llm_cfg.get("base_url", cfg.llm.base_url)
        cfg.llm.max_tokens = int(llm_cfg.get("max_tokens", cfg.llm.max_tokens))
        cfg.llm.temperature = float(llm_cfg.get("temperature", cfg.llm.temperature))
        cfg.llm.timeout = int(llm_cfg.get("timeout", cfg.llm.timeout))

    if "notifier" in raw:
        n = raw["notifier"]
        cfg.notifier.provider = n.get("provider", cfg.notifier.provider)
        cfg.notifier.token = n.get("token", cfg.notifier.token)
        cfg.notifier.chat_id = n.get("chat_id", cfg.notifier.chat_id)
        cfg.notifier.webhook_url = n.get("webhook_url", cfg.notifier.webhook_url)

    if "publisher" in raw:
        pb = raw["publisher"]
        cfg.publisher.provider = pb.get("provider", cfg.publisher.provider)
        cfg.publisher.api_key = pb.get("api_key", cfg.publisher.api_key)
        cfg.publisher.base_url = pb.get("base_url", cfg.publisher.base_url)
        cfg.publisher.output_dir = pb.get("output_dir", cfg.publisher.output_dir)


def _apply_env(cfg: NOVAConfig) -> None:
    """Override config fields from environment variables."""
    env = os.environ.get

    if v := env("NOVA_WORKSPACE"):
        cfg.workspace = v
    if v := env("NOVA_HARNESSES_DIR"):
        cfg.harnesses_dir = v
    if v := env("NOVA_PHASE_TIMEOUT"):
        cfg.phase_timeout = int(v)
    if v := env("NOVA_MAX_RETRIES"):
        cfg.max_retries = int(v)
    if v := env("NOVA_QUALITY_THRESHOLD"):
        cfg.quality_threshold = int(v)
    if v := env("NOVA_DRY_RUN"):
        cfg.dry_run = v.lower() in ("true", "1", "yes")

    # LLM
    if v := env("NOVA_LLM_PROVIDER"):
        cfg.llm.provider = v
    if v := env("NOVA_LLM_MODEL"):
        cfg.llm.model = v
    if v := env("NOVA_LLM_API_KEY"):
        cfg.llm.api_key = v
    if v := env("NOVA_LLM_BASE_URL"):
        cfg.llm.base_url = v
    if v := env("NOVA_LLM_MAX_TOKENS"):
        cfg.llm.max_tokens = int(v)
    if v := env("NOVA_LLM_TEMPERATURE"):
        cfg.llm.temperature = float(v)
    if v := env("NOVA_LLM_TIMEOUT"):
        cfg.llm.timeout = int(v)

    # Notifier
    if v := env("NOVA_NOTIFIER_PROVIDER"):
        cfg.notifier.provider = v
    if v := env("NOVA_NOTIFIER_TOKEN"):
        cfg.notifier.token = v
    if v := env("NOVA_NOTIFIER_CHAT_ID"):
        cfg.notifier.chat_id = v
    if v := env("NOVA_NOTIFIER_WEBHOOK_URL"):
        cfg.notifier.webhook_url = v

    # Publisher
    if v := env("NOVA_PUBLISHER_PROVIDER"):
        cfg.publisher.provider = v
    if v := env("NOVA_PUBLISHER_API_KEY"):
        cfg.publisher.api_key = v
    if v := env("NOVA_PUBLISHER_BASE_URL"):
        cfg.publisher.base_url = v
    if v := env("NOVA_PUBLISHER_OUTPUT_DIR"):
        cfg.publisher.output_dir = v

    # KB
    if v := env("NOVA_KB_PATH"):
        cfg.kb.path = v
    if v := env("NOVA_KB_EMBEDDING_ENABLED"):
        cfg.kb.embedding_enabled = v.lower() in ("true", "1", "yes")
    if v := env("NOVA_KB_EMBEDDING_MODEL"):
        cfg.kb.embedding_model = v
    if v := env("NOVA_KB_EMBEDDING_BASE_URL"):
        cfg.kb.embedding_base_url = v
    if v := env("NOVA_KB_EMBEDDING_API_KEY") or env("NOVA_LLM_API_KEY"):
        cfg.kb.embedding_api_key = v

    # Codex (HMG gpt-5.4)
    if v := env("NOVA_CODEX_API_KEY"):
        cfg.codex.api_key = v
    if v := env("NOVA_CODEX_BASE_URL"):
        cfg.codex.base_url = v
    if v := env("NOVA_CODEX_MODEL"):
        cfg.codex.model = v

    # Image Gen (HMG Gemini)
    if v := env("NOVA_IMAGE_GEN_API_KEY") or env("NOVA_LLM_API_KEY"):
        cfg.image_gen.api_key = v
    if v := env("NOVA_IMAGE_GEN_BASE_URL"):
        cfg.image_gen.base_url = v
    if v := env("NOVA_IMAGE_GEN_MODEL"):
        cfg.image_gen.model = v
    if v := env("NOVA_IMAGE_GEN_FALLBACK_MODEL"):
        cfg.image_gen.fallback_model = v

