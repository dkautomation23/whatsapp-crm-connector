"""The HTTP surface, driven the way Meta drives it."""

import json

import pytest
from conftest import load_sample

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """A fresh app instance with its own database, in demo mode."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("META_APP_SECRET", "api_test_secret")
    monkeypatch.setenv("META_VERIFY_TOKEN", "api_verify_token")
    monkeypatch.setenv("META_ACCESS_TOKEN", "TEST")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "106540352242922")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "api.sqlite3"))

    import importlib

    import httpx

    from wa_connector import app as app_module, config

    config.get_settings.cache_clear()
    importlib.reload(app_module)

    from wa_connector.models import Company

    app_module.store.upsert_company(Company("comp_nordwind", "Nordwind Supplies GmbH"))
    app_module.store.create_contact("491701110001", "Anna Weber", "comp_nordwind", False)

    async with app_module.app.router.lifespan_context(app_module.app):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            http_client.app_module = app_module        # handy for assertions
            yield http_client


def signed(body: bytes) -> dict[str, str]:
    from wa_connector.signature import sign

    return {"X-Hub-Signature-256": sign(body, "api_test_secret"), "Content-Type": "application/json"}


async def test_subscription_handshake_returns_the_challenge(client):
    response = await client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "api_verify_token", "hub.challenge": "1158201444"},
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_handshake_with_the_wrong_token_is_403(client):
    response = await client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"}
    )
    assert response.status_code == 403


async def test_unsigned_webhook_is_rejected_and_recorded(client):
    body = json.dumps(load_sample("01_inbound_known_contact.json")).encode()
    response = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})

    assert response.status_code == 403
    assert any(f["stage"] == "signature" for f in client.app_module.store.failures())


async def test_signed_webhook_is_acked_immediately_and_processed(client):
    body = json.dumps(load_sample("01_inbound_known_contact.json")).encode()
    response = await client.post("/webhook", content=body, headers=signed(body))

    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "statuses": 0}

    await client.app_module.worker.drain()
    stats = client.app_module.worker.stats
    assert (stats.stored, stats.duplicates, stats.failed) == (1, 0, 0)


async def test_redelivery_does_not_create_a_second_entry(client):
    body = json.dumps(load_sample("01_inbound_known_contact.json")).encode()
    await client.post("/webhook", content=body, headers=signed(body))
    await client.post("/webhook", content=body, headers=signed(body))
    await client.app_module.worker.drain()

    assert client.app_module.worker.stats.duplicates == 1
    timeline = (await client.get("/companies/comp_nordwind/conversation")).json()
    assert len(timeline["entries"]) == 1


async def test_company_timeline_collects_several_contacts(client):
    client.app_module.store.create_contact("491701110002", "Jonas Keller", "comp_nordwind", False)
    for sample, minutes in [("01_inbound_known_contact.json", 90), ("02_inbound_same_company_colleague.json", 75)]:
        body = json.dumps(load_sample(sample, minutes)).encode()
        await client.post("/webhook", content=body, headers=signed(body))
    await client.app_module.worker.drain()

    timeline = (await client.get("/companies/comp_nordwind/conversation")).json()
    assert len(timeline["contacts"]) == 2
    assert len(timeline["entries"]) == 2


async def test_malformed_json_returns_400_not_500(client):
    body = b"{not json"
    response = await client.post("/webhook", content=body, headers=signed(body))
    assert response.status_code == 400


async def test_health_exposes_token_queue_and_review_counters(client):
    body = json.dumps(load_sample("03_inbound_unknown_number.json")).encode()
    await client.post("/webhook", content=body, headers=signed(body))
    await client.app_module.worker.drain()

    health = (await client.get("/health")).json()
    assert health["demo_mode"] is True
    assert health["missing_env_for_live"] == []
    assert health["token"]["status"] == "ok"
    assert health["queue"]["stored"] == 1
    assert health["contacts_awaiting_review"] == 1
