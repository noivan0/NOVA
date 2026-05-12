"""
nova.kb — Agent Knowledge Base module for NOVA framework.

Implements the Agent KB Pattern:
  https://gist.github.com/noivan0/agent-kb-pattern

Components:
  sync    — incremental embed + index KB pages into SQLite
  search  — hybrid BM25 + cosine search across KB namespaces
  manager — read/write KB pages with frontmatter validation
  schema  — SCHEMA.md parser + tag taxonomy enforcement
"""

from nova.kb.manager import KBManager
from nova.kb.search import KBSearch
from nova.kb.sync import KBSync

__all__ = ["KBManager", "KBSearch", "KBSync"]
__version__ = "0.1.0"
