"""Sending messages back, with the rule that trips up most integrations:
outside the 24-hour customer service window only approved templates may be sent.

Meta rejects free-form text after 24 hours of customer silence. Discovering
that in production means silently unanswered customers, so the window is
checked here, before the API call, and the caller is told which template to use.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from .config import Settings
from .models import Contact
from .store import Store

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class SendRejected(RuntimeError):
    """The message was not sent, and the reason is actionable by the caller."""


@dataclass(slots=True)
class SendResult:
    ok: bool
    mode: str  # "text" inside the window, "template" outside it
    message_id: str = ""
    detail: str = ""
    attempts: int = 1
    request: dict[str, Any] = field(default_factory=dict)


def window_state(store: Store, contact: Contact, now: datetime | None = None) -> dict[str, Any]:
    """Is the free-form window still open for this contact, and for how long?"""
    now = now or datetime.now(timezone.utc)
    last_inbound = store.last_inbound_at(contact.contact_id)
    if last_inbound is None:
        return {"open": False, "reason": "no inbound message from this contact yet"}
    closes_at = last_inbound + WINDOW
    remaining = closes_at - now
    return {
        "open": remaining.total_seconds() > 0,
        "last_inbound_at": last_inbound.isoformat().replace("+00:00", "Z"),
        "closes_at": closes_at.isoformat().replace("+00:00", "Z"),
        "hours_left": round(remaining.total_seconds() / 3600, 1),
    }


def build_payload(
    to_phone: str, text: str, template: str | None, language: str, inside_window: bool
) -> dict[str, Any]:
    """Exactly the JSON body the Cloud API expects, for either mode."""
    if inside_window:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "template",
        "template": {"name": template, "language": {"code": language}},
    }


class Sender:
    """Posts to the Cloud API with retries; records what it sent in DEMO mode."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        transport: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        # Injectable transport keeps the retry logic testable without network.
        self.transport = transport or self._http_post
        self.sent: list[dict[str, Any]] = []

    def send(
        self,
        contact: Contact,
        text: str,
        template: str | None = None,
        language: str = "en_US",
        now: datetime | None = None,
    ) -> SendResult:
        """Send `text` if the window is open, otherwise the named template."""
        state = window_state(self.store, contact, now)
        inside = bool(state["open"])
        if not inside and not template:
            raise SendRejected(
                f"24h window closed ({state.get('reason') or state.get('closes_at')}); "
                "an approved template name is required"
            )

        payload = build_payload(contact.phone, text, template, language, inside)
        url = f"{self.settings.graph_url}/{self.settings.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.settings.access_token}"}

        if self.settings.demo_mode:
            # No network in demo mode: record the exact request that would go out.
            self.sent.append({"url": url, "payload": payload})
            message_id = f"wamid.demo{len(self.sent):04d}"
            self._log_outbound(contact, payload, message_id, now)
            return SendResult(True, "text" if inside else "template", message_id, "demo", 1, payload)

        return self._send_live(contact, url, payload, headers, inside, now)

    # -- internals ---------------------------------------------------------

    def _send_live(self, contact, url, payload, headers, inside, now) -> SendResult:
        last_detail = ""
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                response = self.transport(url, payload, headers)
                status = response.status_code
                if status < 300:
                    body = response.json()
                    message_id = (body.get("messages") or [{}])[0].get("id", "")
                    self._log_outbound(contact, payload, message_id, now)
                    return SendResult(True, "text" if inside else "template", message_id, "sent", attempt, payload)
                last_detail = f"HTTP {status}: {response.text[:200]}"
                if status not in RETRYABLE_STATUS:
                    break  # 400/401/403 will fail the same way forever
            except requests.RequestException as exc:
                last_detail = f"{type(exc).__name__}: {exc}"

            if attempt < self.settings.max_retries:
                pause = self.settings.retry_base_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                log.warning("send failed (%s), retry %s in %.1fs", last_detail, attempt, pause)
                time.sleep(pause)

        self.store.record_failure("outbound", contact.contact_id, last_detail)
        return SendResult(False, "text" if inside else "template", "", last_detail, self.settings.max_retries, payload)

    def _log_outbound(self, contact: Contact, payload: dict[str, Any], message_id: str, now) -> None:
        body = payload.get("text", {}).get("body") or f"[template: {payload['template']['name']}]"
        occurred = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        self.store.log_message(contact, "outbound", body, message_id or "unknown", occurred)

    def _http_post(self, url: str, payload: dict[str, Any], headers: dict[str, str]):
        return requests.post(url, json=payload, headers=headers, timeout=20)
