"""The 24-hour window, retry behaviour and the token lifecycle."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests
from conftest import load_sample

from wa_connector.crm import handle_inbound
from wa_connector.models import parse_webhook
from wa_connector.outbound import SendRejected, Sender, build_payload, window_state
from wa_connector.tokens import TokenManager


@pytest.fixture
def anna(crm, store):
    messages, _ = parse_webhook(load_sample("01_inbound_known_contact.json", minutes_ago=60))
    handle_inbound(messages[0], crm, store)
    return store.find_contact_by_phone("491701110001")


def test_window_is_open_an_hour_after_the_customer_wrote(store, anna):
    state = window_state(store, anna)
    assert state["open"] is True
    assert 22 < state["hours_left"] <= 23.1


def test_window_is_closed_after_24_hours(store, anna):
    state = window_state(store, anna, datetime.now(timezone.utc) + timedelta(hours=30))
    assert state["open"] is False


def test_contact_who_never_wrote_has_no_window(store, crm):
    jonas = store.find_contact_by_phone("491701110002")
    assert window_state(store, jonas)["open"] is False
    assert "no inbound message" in window_state(store, jonas)["reason"]


def test_free_form_send_inside_the_window(settings, store, anna):
    result = Sender(settings, store).send(anna, "Yes, 12 units are in stock.")
    assert result.ok and result.mode == "text"
    assert result.request["text"]["body"].startswith("Yes, 12 units")


def test_free_form_send_outside_the_window_is_refused(settings, store, anna):
    late = datetime.now(timezone.utc) + timedelta(hours=30)
    with pytest.raises(SendRejected, match="template"):
        Sender(settings, store).send(anna, "Just following up", now=late)


def test_template_send_outside_the_window_is_allowed(settings, store, anna):
    late = datetime.now(timezone.utc) + timedelta(hours=30)
    result = Sender(settings, store).send(anna, "", template="shipping_update", now=late)
    assert result.ok and result.mode == "template"
    assert result.request["template"]["name"] == "shipping_update"


def test_outbound_is_written_to_the_company_timeline(settings, store, anna):
    Sender(settings, store).send(anna, "Reserved for you.")
    entries = store.conversation_for_company("comp_nordwind")
    assert [entry.direction for entry in entries] == ["inbound", "outbound"]


def test_payload_shape_matches_the_cloud_api():
    text = build_payload("491701110001", "hi", None, "en_US", inside_window=True)
    template = build_payload("491701110001", "", "shipping_update", "de_DE", inside_window=False)
    assert text["type"] == "text" and text["messaging_product"] == "whatsapp"
    assert template["type"] == "template"
    assert template["template"]["language"]["code"] == "de_DE"


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def test_transient_failures_are_retried_then_succeed(settings, store, anna):
    live = replace(settings, demo_mode=False)
    responses = [FakeResponse(503, text="busy"), FakeResponse(200, {"messages": [{"id": "wamid.live1"}]})]
    calls = []

    def transport(url, payload, headers):
        calls.append(url)
        return responses.pop(0)

    result = Sender(live, store, transport=transport).send(anna, "hello")
    assert result.ok and result.message_id == "wamid.live1"
    assert len(calls) == 2


def test_permanent_failure_is_not_retried_and_is_logged(settings, store, anna):
    live = replace(settings, demo_mode=False)
    calls = []

    def transport(url, payload, headers):
        calls.append(url)
        return FakeResponse(401, text="invalid token")

    result = Sender(live, store, transport=transport).send(anna, "hello")
    assert result.ok is False
    assert len(calls) == 1                                   # 401 will never succeed
    assert any(failure["stage"] == "outbound" for failure in store.failures())


def test_network_errors_are_retried(settings, store, anna):
    live = replace(settings, demo_mode=False)
    attempts = {"n": 0}

    def transport(url, payload, headers):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.ConnectionError("boom")
        return FakeResponse(200, {"messages": [{"id": "wamid.live2"}]})

    result = Sender(live, store, transport=transport).send(anna, "hello")
    assert result.ok and attempts["n"] == 3


# --- tokens ---------------------------------------------------------------


def test_token_is_recorded_without_storing_the_secret(settings, store):
    manager = TokenManager(settings, store)
    state = manager.remember("EAAG_super_secret_value", 60 * 86400)
    assert state.token_tail == "_value"
    assert "EAAG_super_secret_value" not in str(store.get_state("meta_token"))


def test_refresh_is_due_only_near_expiry(settings, store):
    manager = TokenManager(settings, store)
    manager.remember("token123456", 60 * 86400)
    assert manager.needs_refresh() is False
    assert manager.needs_refresh(datetime.now(timezone.utc) + timedelta(days=55)) is True


def test_missing_token_state_means_refresh_now(settings, store):
    assert TokenManager(settings, store).needs_refresh() is True


def test_failed_refresh_marks_the_connection_expired(settings, store):
    live = replace(settings, demo_mode=False)

    def transport(url, params):
        return FakeResponse(400, {"error": {"code": 190}}, text="OAuthException")

    manager = TokenManager(live, store, transport=transport)
    manager.remember("token123456", 60 * 86400)
    state = manager.refresh()

    assert state.status == "access_expired"
    assert "400" in state.last_error
    assert manager.health()["status"] == "access_expired"
    assert any(failure["stage"] == "token" for failure in store.failures())


def test_successful_refresh_clears_the_expired_flag(settings, store):
    live = replace(settings, demo_mode=False)
    manager = TokenManager(live, store)
    manager.mark_expired("previous failure")

    manager.transport = lambda url, params: FakeResponse(200, {"access_token": "NEWTOKEN12", "expires_in": 5184000})
    state = manager.refresh()

    assert state.status == "ok"
    assert manager.health()["days_left"] > 55
