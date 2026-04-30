"""
nova/providers/notifier.py
--------------------------
Pluggable notification provider abstraction.

Supported providers:
  none     — Silent (no notifications)
  telegram — Telegram Bot API
  slack    — Slack Incoming Webhooks
  discord  — Discord Webhooks
  webhook  — Generic HTTP POST webhook

Usage:
  from nova.providers.notifier import get_notifier
  notifier = get_notifier(config.notifier)
  notifier.send("Harness completed successfully!")
"""

from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod

from nova.core.config import NotifierConfig


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str) -> bool:
        """Send a notification. Returns True on success."""
        ...


# --------------------------------------------------------------------------- #
# None (silent)
# --------------------------------------------------------------------------- #

class NullNotifier(Notifier):
    def send(self, message: str) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

class TelegramNotifier(Notifier):
    """
    Sends messages via Telegram Bot API.
    Requires: token (bot token), chat_id (channel or group ID)
    """

    def __init__(self, cfg: NotifierConfig):
        self.token = cfg.token
        self.chat_id = cfg.chat_id

    def send(self, message: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"[notifier/telegram] Failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #

class SlackNotifier(Notifier):
    """Sends messages via Slack Incoming Webhook."""

    def __init__(self, cfg: NotifierConfig):
        self.webhook_url = cfg.webhook_url

    def send(self, message: str) -> bool:
        payload = json.dumps({"text": message}).encode()
        try:
            req = urllib.request.Request(
                self.webhook_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"[notifier/slack] Failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

class DiscordNotifier(Notifier):
    """Sends messages via Discord Webhook."""

    def __init__(self, cfg: NotifierConfig):
        self.webhook_url = cfg.webhook_url

    def send(self, message: str) -> bool:
        payload = json.dumps({"content": message}).encode()
        try:
            req = urllib.request.Request(
                self.webhook_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"[notifier/discord] Failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Generic Webhook
# --------------------------------------------------------------------------- #

class WebhookNotifier(Notifier):
    """Sends a JSON POST to any HTTP endpoint."""

    def __init__(self, cfg: NotifierConfig):
        self.webhook_url = cfg.webhook_url

    def send(self, message: str) -> bool:
        payload = json.dumps({"message": message}).encode()
        try:
            req = urllib.request.Request(
                self.webhook_url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            print(f"[notifier/webhook] Failed: {e}")
            return False


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_notifier(cfg: NotifierConfig) -> Notifier:
    """
    Instantiate the correct notifier from config.

    Provider selection:
      - "none"     → NullNotifier (silent)
      - "telegram" → TelegramNotifier
      - "slack"    → SlackNotifier
      - "discord"  → DiscordNotifier
      - "webhook"  → WebhookNotifier
    """
    p = cfg.provider.lower()

    if p == "none":
        return NullNotifier()
    elif p == "telegram":
        return TelegramNotifier(cfg)
    elif p == "slack":
        return SlackNotifier(cfg)
    elif p == "discord":
        return DiscordNotifier(cfg)
    elif p == "webhook":
        return WebhookNotifier(cfg)
    else:
        raise ValueError(
            f"Unknown notifier provider: '{cfg.provider}'. "
            f"Valid options: none, telegram, slack, discord, webhook"
        )
