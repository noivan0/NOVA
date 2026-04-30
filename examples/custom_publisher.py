"""
examples/custom_publisher.py
------------------------------
How to implement and register a custom Publisher.

Use this when:
- You want to publish to a platform not yet supported (Medium, Substack, Notion …)
- You need a custom API integration for your CMS

Usage:
    1. Copy this file and implement MyPublisher.publish()
    2. Register it in nova/providers/publisher.py -> get_publisher()
    3. Set NOVA_PUBLISHER_PROVIDER=my-publisher in nova.yaml or .env
"""

from nova.providers.publisher import Publisher
from nova.core.config import PublisherConfig
from typing import List, Optional
import json
import urllib.request


class MyPublisher(Publisher):
    """
    Example custom publisher for any CMS or platform.

    Configure in nova.yaml:
        publisher:
          provider: my-publisher
          base_url: https://my-cms.example.com
          api_key: my-api-key

    Or via environment variables:
        NOVA_PUBLISHER_PROVIDER=my-publisher
        NOVA_PUBLISHER_BASE_URL=https://my-cms.example.com
        NOVA_PUBLISHER_API_KEY=my-api-key
    """

    def __init__(self, cfg: PublisherConfig):
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.api_key = cfg.api_key

    def publish(
        self,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Publish content and return the public URL, or None on failure.

        Return value:
          - str  → the published URL; NOVA logs it in the evolution log
          - None → publish failed or not applicable
        """
        payload = json.dumps({
            "title": title,
            "content": content,
            "tags": tags or [],
            **(metadata or {}),
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/api/posts",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                url = data.get("url") or data.get("link")
                print(f"[publisher/my-publisher] Published: {url}")
                return url
        except Exception as e:
            print(f"[publisher/my-publisher] Failed: {e}")
            return None


# ── Registration ─────────────────────────────────────────────────────────────
# Add to nova/providers/publisher.py -> get_publisher():
#
#   from examples.custom_publisher import MyPublisher
#
#   def get_publisher(cfg: PublisherConfig) -> Publisher:
#       if cfg.provider == "my-publisher":
#           return MyPublisher(cfg)
#       ...
# ─────────────────────────────────────────────────────────────────────────────
