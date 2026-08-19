"""Shapes of the data that moves through the connector.

The inbound webhook is parsed permissively: Meta adds fields over time, and a
schema that rejects unknown keys turns a harmless platform update into lost
customer messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class InboundMessage:
    """One customer message, flattened out of Meta's nested webhook envelope."""

    message_id: str
    from_phone: str
    to_phone_number_id: str
    text: str
    message_type: str
    timestamp: str
    profile_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sent_at(self) -> datetime:
        """Meta sends a unix timestamp as a string."""
        try:
            return datetime.fromtimestamp(int(self.timestamp), tz=timezone.utc)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


@dataclass(slots=True)
class StatusUpdate:
    """Delivery receipt for a message we sent (sent / delivered / read / failed)."""

    message_id: str
    status: str
    recipient: str
    timestamp: str
    error: str = ""


@dataclass(slots=True)
class Contact:
    contact_id: str
    phone: str
    name: str
    company_id: str | None = None
    needs_review: bool = False
    created_at: str = field(default_factory=_utc_now)


@dataclass(slots=True)
class Company:
    company_id: str
    name: str
    domain: str = ""


@dataclass(slots=True)
class ConversationEntry:
    """A message written into the CRM timeline.

    `company_id` is populated for every entry whose contact belongs to a
    company - that is the whole point of this connector: a sales manager
    opening the *company* record sees the WhatsApp thread, not just whoever
    happened to be the contact.
    """

    entry_id: str
    contact_id: str
    company_id: str | None
    direction: str  # "inbound" | "outbound"
    body: str
    message_id: str
    occurred_at: str
    channel: str = "whatsapp"


def parse_webhook(payload: dict[str, Any]) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    """Flatten Meta's entry -> changes -> value -> messages/statuses envelope.

    Unknown or partial structures are skipped instead of raising: one malformed
    entry in a batch must not drop the other messages in the same request.
    """
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []

    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id", "")

            profiles = {
                contact.get("wa_id", ""): (contact.get("profile") or {}).get("name", "")
                for contact in value.get("contacts", []) or []
            }

            for message in value.get("messages", []) or []:
                message_id = message.get("id")
                sender = message.get("from")
                if not message_id or not sender:
                    continue
                message_type = message.get("type", "unknown")
                messages.append(
                    InboundMessage(
                        message_id=message_id,
                        from_phone=sender,
                        to_phone_number_id=phone_number_id,
                        text=_extract_text(message, message_type),
                        message_type=message_type,
                        timestamp=message.get("timestamp", ""),
                        profile_name=profiles.get(sender, ""),
                        raw=message,
                    )
                )

            for status in value.get("statuses", []) or []:
                if not status.get("id"):
                    continue
                errors = status.get("errors") or []
                statuses.append(
                    StatusUpdate(
                        message_id=status["id"],
                        status=status.get("status", "unknown"),
                        recipient=status.get("recipient_id", ""),
                        timestamp=status.get("timestamp", ""),
                        error=(errors[0].get("title", "") if errors else ""),
                    )
                )

    return messages, statuses


def _extract_text(message: dict[str, Any], message_type: str) -> str:
    """Readable body for the CRM timeline, whatever the message type is."""
    if message_type == "text":
        return (message.get("text") or {}).get("body", "")
    if message_type == "button":
        return (message.get("button") or {}).get("text", "")
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title", "")
    if message_type in {"image", "document", "audio", "video"}:
        media = message.get(message_type) or {}
        caption = media.get("caption", "")
        return f"[{message_type}] {caption}".strip()
    return f"[{message_type}]"
