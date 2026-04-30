"""
nova/providers/publisher.py
---------------------------
Pluggable content publisher abstraction.

Supported providers:
  none       — No publishing (output stays in workspace/)
  file       — Write to local filesystem (great for static sites)
  wordpress  — WordPress REST API
  ghost      — Ghost Content API

Usage:
  from nova.providers.publisher import get_publisher
  publisher = get_publisher(config.publisher)
  url = publisher.publish(title="My Post", content="<h1>Hello</h1>", tags=["ai"])
"""

from __future__ import annotations

import json
import urllib.request
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from nova.core.config import PublisherConfig


class Publisher(ABC):
    @abstractmethod
    def publish(
        self,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Publish content. Returns the published URL (or file path) on success,
        None on failure.
        """
        ...


# --------------------------------------------------------------------------- #
# None (local only)
# --------------------------------------------------------------------------- #

class NullPublisher(Publisher):
    """Content is not published — stays in the workspace directory."""

    def publish(self, title, content, tags=None, metadata=None) -> Optional[str]:
        print(f"[publisher/none] Not publishing: {title!r}")
        return None


# --------------------------------------------------------------------------- #
# File (local filesystem / static site)
# --------------------------------------------------------------------------- #

class FilePublisher(Publisher):
    """
    Writes content to a local directory.
    Useful for static site generators (Hugo, Jekyll, Docusaurus, etc.)
    """

    def __init__(self, cfg: PublisherConfig):
        self.output_dir = Path(cfg.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def publish(self, title, content, tags=None, metadata=None) -> Optional[str]:
        safe_title = title.lower().replace(" ", "-").replace("/", "-")[:80]
        out_path = self.output_dir / f"{safe_title}.md"
        front_matter = f"---\ntitle: {title}\ntags: {tags or []}\n---\n\n"
        out_path.write_text(front_matter + content)
        print(f"[publisher/file] Written: {out_path}")
        return str(out_path)


# --------------------------------------------------------------------------- #
# WordPress
# --------------------------------------------------------------------------- #

class WordPressPublisher(Publisher):
    """
    Publishes via WordPress REST API.
    Requires: base_url (site URL), api_key (app password as "user:password")
    """

    def __init__(self, cfg: PublisherConfig):
        self.base_url = cfg.base_url.rstrip("/")
        # api_key format: "username:application_password"
        import base64
        creds = base64.b64encode(cfg.api_key.encode()).decode()
        self.auth_header = f"Basic {creds}"

    def publish(self, title, content, tags=None, metadata=None) -> Optional[str]:
        url = f"{self.base_url}/wp-json/wp/v2/posts"
        payload = json.dumps({
            "title": title,
            "content": content,
            "status": "publish",
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": self.auth_header,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get("link")
        except Exception as e:
            print(f"[publisher/wordpress] Failed: {e}")
            return None


# --------------------------------------------------------------------------- #
# Ghost
# --------------------------------------------------------------------------- #

class GhostPublisher(Publisher):
    """
    Publishes via Ghost Content API (Admin API key required).
    Requires: base_url (Ghost site URL), api_key ("id:secret" Admin API key)
    """

    def __init__(self, cfg: PublisherConfig):
        self.base_url = cfg.base_url.rstrip("/")
        self.api_key = cfg.api_key  # "id:secret"

    def _get_jwt(self) -> str:
        import hmac, hashlib, base64, time
        key_id, secret = self.api_key.split(":")
        now = int(time.time())
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "kid": key_id, "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": now + 300, "iat": now, "aud": "/admin/"}).encode()
        ).rstrip(b"=").decode()
        sig_input = f"{header}.{payload}".encode()
        sig = hmac.HMAC(bytes.fromhex(secret), sig_input, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        return f"{header}.{payload}.{sig_b64}"

    def publish(self, title, content, tags=None, metadata=None) -> Optional[str]:
        url = f"{self.base_url}/ghost/api/admin/posts/?source=html"
        post_tags = [{"name": t} for t in (tags or [])]
        payload = json.dumps({"posts": [{
            "title": title, "html": content, "tags": post_tags, "status": "published"
        }]}).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Ghost {self._get_jwt()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["posts"][0].get("url")
        except Exception as e:
            print(f"[publisher/ghost] Failed: {e}")
            return None


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_publisher(cfg: PublisherConfig) -> Publisher:
    """
    Instantiate the correct publisher from config.

    Provider selection:
      - "none"      → NullPublisher
      - "file"      → FilePublisher
      - "wordpress" → WordPressPublisher
      - "ghost"     → GhostPublisher
    """
    p = cfg.provider.lower()

    if p == "none":
        return NullPublisher()
    elif p == "file":
        return FilePublisher(cfg)
    elif p == "wordpress":
        return WordPressPublisher(cfg)
    elif p == "ghost":
        return GhostPublisher(cfg)
    else:
        raise ValueError(
            f"Unknown publisher provider: '{cfg.provider}'. "
            f"Valid options: none, file, wordpress, ghost"
        )
