"""Shared fixtures: a temporary store, a seeded CRM and demo settings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wa_connector.config import Settings
from wa_connector.crm import LocalCrm
from wa_connector.models import Company
from wa_connector.store import Store

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
APP_SECRET = "test_app_secret"  # pragma: allowlist secret - demo placeholder, not a real credential


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_secret=APP_SECRET,
        verify_token="test_verify_token",  # pragma: allowlist secret - demo placeholder, not a real credential
        access_token="TEST_TOKEN",  # pragma: allowlist secret - demo placeholder, not a real credential
        phone_number_id="106540352242922",
        demo_mode=True,
        state_path=str(tmp_path / "state.sqlite3"),
        max_retries=3,
        retry_base_seconds=0.0,
    )


@pytest.fixture
def store(settings) -> Store:
    store = Store(settings.state_path)
    store.upsert_company(Company("comp_nordwind", "Nordwind Supplies GmbH", "nordwind.example"))
    store.create_contact("491701110001", "Anna Weber", company_id="comp_nordwind", needs_review=False)
    store.create_contact("491701110002", "Jonas Keller", company_id="comp_nordwind", needs_review=False)
    return store


@pytest.fixture
def crm(store) -> LocalCrm:
    return LocalCrm(store)


def load_sample(name: str, minutes_ago: int = 60) -> dict:
    """Sample webhook with its timestamps moved to `minutes_ago` from now."""
    payload = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    stamp = str(int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp()))
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in list(value.get("messages", [])) + list(value.get("statuses", [])):
                item["timestamp"] = stamp
    return payload
