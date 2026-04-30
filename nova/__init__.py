"""
NOVA — Noivan Orchestrated Vanguard Architecture
A single-agent AI orchestration framework for autonomous project execution.

Core concepts:
  - Harness  : declarative YAML workflow definition
  - Chain    : ordered phase execution with checkpointing
  - Provider : pluggable LLM / Notifier / Publisher backends
  - KB       : persistent knowledge base (markdown-based)
  - Evolution: self-improvement log per harness run
"""

__version__ = "1.0.0"
__author__ = "noivan"
__license__ = "MIT"
