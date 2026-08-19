"""Matching a phone number to a CRM contact, and writing the thread to both
the contact and its company.

Everything talks to the `CrmClient` protocol, so swapping the demo store for
HubSpot / Pipedrive / a bespoke REST CRM is one adapter, not a rewrite.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import Company, Contact, ConversationEntry, InboundMessage
from .store import Store, normalise_phone

log = logging.getLogger(__name__)


class CrmClient(Protocol):
    """The four operations a CRM has to support for this connector."""

    def find_contact(self, phone: str) -> Contact | None: ...

    def create_contact(self, phone: str, name: str) -> Contact: ...

    def log_conversation(
        self, contact: Contact, direction: str, body: str, message_id: str, occurred_at: str
    ) -> ConversationEntry: ...

    def company_of(self, contact: Contact) -> Company | None: ...


class LocalCrm:
    """CRM adapter backed by the local SQLite store (used by the demo and tests).

    A real adapter replaces the bodies with HTTP calls; the behaviour that
    matters - matching, review flags, dual logging - lives in `handle_inbound`
    below and stays identical.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def find_contact(self, phone: str) -> Contact | None:
        return self.store.find_contact_by_phone(phone)

    def create_contact(self, phone: str, name: str) -> Contact:
        # A number nobody recognises becomes a contact immediately (so the
        # message is never orphaned) and is flagged for a human to merge or
        # attach to the right company.
        return self.store.create_contact(phone, name, company_id=None, needs_review=True)

    def log_conversation(
        self, contact: Contact, direction: str, body: str, message_id: str, occurred_at: str
    ) -> ConversationEntry:
        return self.store.log_message(contact, direction, body, message_id, occurred_at)

    def company_of(self, contact: Contact) -> Company | None:
        return self.store.company(contact.company_id) if contact.company_id else None


def handle_inbound(message: InboundMessage, crm: CrmClient, store: Store) -> dict[str, object]:
    """Process one customer message end to end. Idempotent by message id.

    Returns a small dict describing what happened - the demo prints it and the
    tests assert on it.
    """
    if store.already_processed(message.message_id):
        # Meta re-delivers when an ACK is slow or a deploy restarts the process.
        log.info("duplicate webhook ignored: %s", message.message_id)
        return {"status": "duplicate", "message_id": message.message_id}

    phone = normalise_phone(message.from_phone)
    contact = crm.find_contact(phone)
    created = False
    if contact is None:
        contact = crm.create_contact(phone, message.profile_name)
        created = True
        log.info("unknown number %s -> new contact %s (flagged for review)", phone, contact.contact_id)

    entry = crm.log_conversation(
        contact,
        direction="inbound",
        body=message.text,
        message_id=message.message_id,
        occurred_at=message.sent_at.isoformat().replace("+00:00", "Z"),
    )
    company = crm.company_of(contact)

    store.mark_processed(message.message_id, "stored")
    return {
        "status": "stored",
        "message_id": message.message_id,
        "contact_id": contact.contact_id,
        "contact_created": created,
        "needs_review": contact.needs_review,
        # The company link is what makes the thread visible on the account
        # record, not only on the person who happened to write.
        "company_id": company.company_id if company else None,
        "company_name": company.name if company else None,
        "entry_id": entry.entry_id,
    }
