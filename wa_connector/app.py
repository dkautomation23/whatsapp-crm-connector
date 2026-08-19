"""FastAPI app: the webhook Meta calls, plus a few operational endpoints.

    uvicorn wa_connector.app:app --reload --port 8000

    GET  /webhook   subscription handshake (hub.challenge)
    POST /webhook   inbound messages and delivery receipts -> queue -> CRM
    GET  /health    token status, queue counters, contacts awaiting review
    GET  /contacts/{id}/conversation
    GET  /companies/{id}/conversation
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, Query, Request, Response

from .config import get_settings
from .crm import LocalCrm
from .models import parse_webhook
from .signature import verify_signature, verify_subscription
from .store import Store
from .tokens import TokenManager
from .worker import InboundWorker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("wa_connector")

settings = get_settings()
store = Store(settings.state_path)
crm = LocalCrm(store)
worker = InboundWorker(crm, store)
tokens = TokenManager(settings, store)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await worker.start()
    if tokens.needs_refresh():
        tokens.refresh()          # in production this also runs on a scheduler
    yield
    await worker.stop()


app = FastAPI(
    title="whatsapp-crm-connector",
    version="0.1.0",
    description="WhatsApp Business Cloud webhooks -> CRM contact + company timeline.",
    lifespan=lifespan,
)


@app.get("/webhook")
def verify(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Meta calls this once when you save the callback URL."""
    if verify_subscription(mode, token, settings.verify_token):
        return Response(content=challenge or "", media_type="text/plain")
    return Response(content="verification failed", status_code=403)


@app.post("/webhook")
async def receive(request: Request, signature: str | None = Header(default=None, alias="X-Hub-Signature-256")):
    """Verify, parse, enqueue, ACK. Nothing slow happens in this handler."""
    raw = await request.body()

    if not verify_signature(raw, signature, settings.app_secret):
        # Do not tell an attacker which half was wrong.
        store.record_failure("signature", signature or "missing", "invalid X-Hub-Signature-256")
        return Response(content='{"error":"invalid signature"}', status_code=403, media_type="application/json")

    try:
        payload = await request.json()
    except ValueError:
        return Response(content='{"error":"invalid json"}', status_code=400, media_type="application/json")

    messages, statuses = parse_webhook(payload)
    for message in messages:
        await worker.enqueue(message)
    for status in statuses:
        worker.record_status(status)

    # 200 immediately - the CRM work continues in the background.
    return {"accepted": len(messages), "statuses": len(statuses)}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "demo_mode": settings.demo_mode,
        "missing_env_for_live": settings.missing_for_live(),
        "token": tokens.health(),
        "queue": worker.stats.as_dict(),
        "contacts_awaiting_review": len(store.contacts_needing_review()),
        "failures": len(store.failures()),
    }


@app.get("/contacts/{contact_id}/conversation")
def contact_conversation(contact_id: str) -> dict[str, Any]:
    entries = store.conversation_for_contact(contact_id)
    return {"contact_id": contact_id, "entries": [asdict(entry) for entry in entries]}


@app.get("/companies/{company_id}/conversation")
def company_conversation(company_id: str) -> dict[str, Any]:
    """Every WhatsApp message from every contact at this company."""
    entries = store.conversation_for_company(company_id)
    return {
        "company_id": company_id,
        "contacts": sorted({entry.contact_id for entry in entries}),
        "entries": [asdict(entry) for entry in entries],
    }
