"""End-to-end demo. No Meta app, no CRM account, no tokens required.

    python run_demo.py

It runs the real FastAPI app in demo mode, signs the sample webhooks exactly the
way Meta does, posts them to the real endpoint, and prints what ended up in the
CRM - including the company timeline, the 24-hour window rule and the token
lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"
DEMO_SECRET = "demo_app_secret"  # pragma: allowlist secret - demo placeholder, not a real credential
DEMO_DB = Path("demo_state.sqlite3")

# Configure before importing the app: settings are read once, at import time.
os.environ.update(
    DEMO_MODE="true",
    META_APP_SECRET=DEMO_SECRET,
    META_VERIFY_TOKEN="demo_verify_token",  # pragma: allowlist secret - demo placeholder, not a real credential
    META_ACCESS_TOKEN="DEMO_ACCESS_TOKEN",  # pragma: allowlist secret - demo placeholder, not a real credential
    WHATSAPP_PHONE_NUMBER_ID="106540352242922",
    STATE_PATH=str(DEMO_DB),
)
if DEMO_DB.exists():
    DEMO_DB.unlink()  # fresh run every time, so the output is reproducible

import httpx  # noqa: E402

from wa_connector.app import app, settings, store, tokens, worker  # noqa: E402

# The demo output is the point here - keep the client's request log out of it.
logging.getLogger("httpx").setLevel(logging.WARNING)
from wa_connector.models import Company  # noqa: E402
from wa_connector.outbound import SendRejected, Sender, window_state  # noqa: E402
from wa_connector.signature import sign  # noqa: E402

RULE = "-" * 74


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def seed_crm() -> None:
    """Pretend the CRM already knows one company with one contact."""
    store.upsert_company(
        Company(company_id="comp_nordwind", name="Nordwind Supplies GmbH", domain="nordwind.example")
    )
    store.create_contact("491701110001", "Anna Weber", company_id="comp_nordwind", needs_review=False)
    store.create_contact("491701110002", "Jonas Keller", company_id="comp_nordwind", needs_review=False)
    print("CRM seeded: Nordwind Supplies GmbH with 2 contacts (Anna Weber, Jonas Keller)")


def freshen(payload: dict, minutes_ago: int) -> bytes:
    """Rewrite the sample's unix timestamps to "N minutes ago".

    The 24-hour window is relative to when the customer last wrote, so a fixture
    frozen in the past would always look expired. Meta sends the real clock;
    this does the same, and keeps the demo output meaningful on any day.
    """
    stamp = str(int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp()))
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in list(value.get("messages", [])) + list(value.get("statuses", [])):
                item["timestamp"] = stamp
    return json.dumps(payload).encode()


async def post_sample(client: httpx.AsyncClient, filename: str, minutes_ago: int = 60) -> None:
    """Send one sample webhook with a valid X-Hub-Signature-256."""
    payload = json.loads((SAMPLES / filename).read_text(encoding="utf-8"))
    body = freshen(payload, minutes_ago)
    response = await client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sign(body, DEMO_SECRET), "Content-Type": "application/json"},
    )
    print(f"  {filename:<42} -> {response.status_code} {response.json()}")


async def main() -> int:
    print("whatsapp-crm-connector demo | DEMO_MODE =", settings.demo_mode)
    seed_crm()

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            head("1. Webhook subscription handshake (what Meta does once)")
            ok = await client.get(
                "/webhook",
                params={"hub.mode": "subscribe", "hub.verify_token": "demo_verify_token", "hub.challenge": "1158201444"},
            )
            bad = await client.get(
                "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "x"}
            )
            print(f"  correct verify token -> {ok.status_code} {ok.text}")
            print(f"  wrong verify token   -> {bad.status_code} {bad.text}")

            head("2. Forged webhook is rejected before anything is parsed")
            forged = await client.post(
                "/webhook",
                content=(SAMPLES / "01_inbound_known_contact.json").read_bytes(),
                headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
            )
            print(f"  bad signature -> {forged.status_code} {forged.json()}")

            head("3. Inbound webhooks (ACKed immediately, processed in the queue)")
            schedule = [
                ("01_inbound_known_contact.json", 90),
                ("02_inbound_same_company_colleague.json", 75),
                ("03_inbound_unknown_number.json", 60),
                ("06_inbound_image_with_caption.json", 45),
                ("04_duplicate_delivery.json", 90),   # Meta re-delivering message #1
                ("05_status_failed.json", 30),
            ]
            for filename, minutes_ago in schedule:
                await post_sample(client, filename, minutes_ago)

            await worker.drain()

            head("4. What the worker did")
            print(json.dumps(worker.stats.as_dict(), indent=2))
            for result in worker.stats.results:
                print("  ", json.dumps(result, ensure_ascii=False))

            head("5. The point of the connector: the thread is on the COMPANY too")
            company_view = (await client.get("/companies/comp_nordwind/conversation")).json()
            print(
                f"  company comp_nordwind: {len(company_view['entries'])} messages "
                f"from {len(company_view['contacts'])} contacts"
            )
            for entry in company_view["entries"]:
                print(f"   [{entry['occurred_at']}] {entry['direction']:<8} {entry['contact_id']}: {entry['body'][:66]}")

            head("6. Unknown numbers are captured and flagged, never dropped")
            for contact in store.contacts_needing_review():
                print(f"   {contact.contact_id}  +{contact.phone}  '{contact.name}'  needs_review=True  company=None")

            head("7. The 24-hour customer service window")
            anna = store.find_contact_by_phone("491701110001")
            marek = store.find_contact_by_phone("48511222333")
            sender = Sender(settings, store)
            now = datetime.now(timezone.utc)

            state = window_state(store, anna, now)
            print(f"   Anna: open={state['open']}, closes_at={state['closes_at']}, hours_left={state['hours_left']}")
            free_form = sender.send(anna, "Yes, 12 units are in stock. Shall I reserve them?", now=now)
            print(f"   free-form send -> ok={free_form.ok} mode={free_form.mode} id={free_form.message_id}")

            late = now + timedelta(hours=30)
            print(f"\n   30 hours later -> window open={window_state(store, marek, late)['open']}")
            try:
                sender.send(marek, "Following up on your question", now=late)
            except SendRejected as exc:
                print(f"   free-form send blocked: {exc}")
            templated = sender.send(marek, "", template="shipping_update", language="en_US", now=late)
            print(
                f"   template send  -> ok={templated.ok} mode={templated.mode} "
                f"template={templated.request['template']['name']}"
            )

            head("8. Token lifecycle")
            print("   health:", json.dumps(tokens.health()))
            expired = tokens.mark_expired("simulated: Meta returned 190 OAuthException")
            print(f"   failed refresh -> status={expired.status}, last_error='{expired.last_error}'")
            print("   /health reports:", json.dumps((await client.get("/health")).json()["token"]))
            tokens.refresh()
            print("   successful refresh -> status =", tokens.state().status)

            head("9. Nothing failed silently")
            for failure in store.failures():
                print(f"   [{failure['happened_at']}] {failure['stage']:<10} {failure['detail'][:64]}")

    print(f"\nDone. State written to {DEMO_DB.resolve()}")
    print("Live mode still needs:", ", ".join(settings.missing_for_live()) or "nothing - all variables set")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
