"""Contact matching, the company link, de-duplication and review flags."""

from conftest import load_sample

from wa_connector.crm import handle_inbound
from wa_connector.models import parse_webhook
from wa_connector.store import normalise_phone


def first_message(sample: str, minutes_ago: int = 60):
    messages, _ = parse_webhook(load_sample(sample, minutes_ago))
    return messages[0]


def test_known_number_is_matched_and_linked_to_its_company(crm, store):
    result = handle_inbound(first_message("01_inbound_known_contact.json"), crm, store)

    assert result["status"] == "stored"
    assert result["contact_created"] is False
    assert result["needs_review"] is False
    assert result["company_id"] == "comp_nordwind"
    assert result["company_name"] == "Nordwind Supplies GmbH"


def test_message_is_written_to_contact_and_company_timelines(crm, store):
    handle_inbound(first_message("01_inbound_known_contact.json"), crm, store)
    contact = store.find_contact_by_phone("491701110001")

    on_contact = store.conversation_for_contact(contact.contact_id)
    on_company = store.conversation_for_company("comp_nordwind")

    assert len(on_contact) == 1
    assert len(on_company) == 1
    assert on_company[0].entry_id == on_contact[0].entry_id


def test_two_colleagues_share_one_company_thread(crm, store):
    handle_inbound(first_message("01_inbound_known_contact.json", 90), crm, store)
    handle_inbound(first_message("02_inbound_same_company_colleague.json", 75), crm, store)

    entries = store.conversation_for_company("comp_nordwind")
    assert len(entries) == 2
    assert len({entry.contact_id for entry in entries}) == 2      # two people, one thread
    assert [entry.occurred_at for entry in entries] == sorted(entry.occurred_at for entry in entries)


def test_unknown_number_creates_a_contact_flagged_for_review(crm, store):
    result = handle_inbound(first_message("03_inbound_unknown_number.json"), crm, store)

    assert result["contact_created"] is True
    assert result["needs_review"] is True
    assert result["company_id"] is None                            # nothing invented
    assert [c.phone for c in store.contacts_needing_review()] == ["48511222333"]
    # The message itself is still stored - never dropped for lack of a contact.
    assert len(store.conversation_for_contact(result["contact_id"])) == 1


def test_redelivery_of_the_same_message_id_is_ignored(crm, store):
    message = first_message("01_inbound_known_contact.json")
    first = handle_inbound(message, crm, store)
    second = handle_inbound(message, crm, store)

    assert first["status"] == "stored"
    assert second["status"] == "duplicate"
    assert len(store.conversation_for_company("comp_nordwind")) == 1


def test_duplicate_sample_from_meta_is_recognised(crm, store):
    handle_inbound(first_message("01_inbound_known_contact.json", 90), crm, store)
    replay = handle_inbound(first_message("04_duplicate_delivery.json", 90), crm, store)
    assert replay["status"] == "duplicate"


def test_phone_normalisation_matches_crm_formats():
    assert normalise_phone("+49 170 555 24 18") == "491705552418"
    assert normalise_phone("0049 (170) 555-24-18") == "491705552418"
    assert normalise_phone("491705552418") == "491705552418"


def test_a_number_stored_in_pretty_format_still_matches(store, crm):
    """The CRM often holds '+49 170 111 0003'; Meta sends '491701110003'."""
    store.create_contact("+49 170 111 0003", "Petra Klein", company_id="comp_nordwind", needs_review=False)
    message = first_message("01_inbound_known_contact.json")
    message.from_phone = "491701110003"
    message.message_id = "wamid.unique.0003"

    result = handle_inbound(message, crm, store)
    assert result["contact_created"] is False
    assert result["company_id"] == "comp_nordwind"
