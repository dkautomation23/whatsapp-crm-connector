"""Webhook authenticity and envelope parsing."""

import json

from conftest import APP_SECRET, load_sample

from wa_connector.models import parse_webhook
from wa_connector.signature import sign, verify_signature, verify_subscription


def test_valid_signature_passes():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(body, sign(body, APP_SECRET), APP_SECRET) is True


def test_tampered_body_fails():
    body = b'{"amount": 100}'
    header = sign(body, APP_SECRET)
    assert verify_signature(b'{"amount": 100000}', header, APP_SECRET) is False


def test_missing_or_malformed_header_fails():
    body = b"{}"
    assert verify_signature(body, None, APP_SECRET) is False
    assert verify_signature(body, "deadbeef", APP_SECRET) is False          # no sha256= prefix
    assert verify_signature(body, sign(body, "other_secret"), APP_SECRET) is False


def test_signature_check_needs_a_configured_secret():
    """An empty app secret must never accidentally accept everything."""
    body = b"{}"
    assert verify_signature(body, sign(body, ""), "") is False


def test_subscription_handshake():
    assert verify_subscription("subscribe", "tok", "tok") is True
    assert verify_subscription("subscribe", "wrong", "tok") is False
    assert verify_subscription("unsubscribe", "tok", "tok") is False
    assert verify_subscription("subscribe", "", "") is False


def test_parse_text_message():
    messages, statuses = parse_webhook(load_sample("01_inbound_known_contact.json"))
    assert statuses == []
    assert len(messages) == 1

    message = messages[0]
    assert message.from_phone == "491701110001"
    assert message.profile_name == "Anna Weber"
    assert message.message_type == "text"
    assert "industrial gateway G2" in message.text
    assert message.to_phone_number_id == "106540352242922"


def test_parse_image_message_keeps_the_caption():
    messages, _ = parse_webhook(load_sample("06_inbound_image_with_caption.json"))
    assert messages[0].text == "[image] This is the socket we need to match"


def test_parse_status_update_with_error():
    messages, statuses = parse_webhook(load_sample("05_status_failed.json"))
    assert messages == []
    assert statuses[0].status == "failed"
    assert "24 hour window" in statuses[0].error


def test_unknown_shapes_are_skipped_not_fatal():
    """Meta adds fields over time - a surprise must not drop the batch."""
    payload = {
        "entry": [
            {"changes": [{"value": {"messages": [{"id": "", "from": "49170"}]}}]},   # no id -> skipped
            {"changes": [{"value": {"messages": [{"id": "wamid.ok", "from": "49170", "type": "reaction"}]}}]},
            {"changes": [{"value": {"something_new": {"foo": "bar"}}}]},
        ]
    }
    messages, statuses = parse_webhook(payload)
    assert [m.message_id for m in messages] == ["wamid.ok"]
    assert messages[0].text == "[reaction]"
    assert statuses == []


def test_signing_matches_the_documented_format():
    body = json.dumps({"a": 1}).encode()
    assert sign(body, APP_SECRET).startswith("sha256=")
    assert len(sign(body, APP_SECRET)) == len("sha256=") + 64
