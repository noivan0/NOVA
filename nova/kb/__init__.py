"""
nova.kb — Agent Knowledge Base module for NOVA framework.

Implements the Agent KB Pattern:
  https://gist.github.com/noivan0/2c1129a2b8d829be70cab1439d4c6e18

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
