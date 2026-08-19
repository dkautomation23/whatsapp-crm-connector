"""Keeping the Meta access token alive, and being honest when it dies.

A WhatsApp integration that "worked for two months and then went quiet" is
almost always an expired long-lived token. Two rules here:

* refresh before expiry, on a schedule, not on the first failed send;
* if the refresh fails, mark the connection `access_expired` loudly instead of
  letting messages pile up in a queue nobody is watching.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

STATE_KEY = "meta_token"
DEMO_LIFETIME_DAYS = 60


@dataclass(slots=True)
class TokenState:
    token_tail: str          # last 6 characters only - never store the token itself here
    expires_at: str
    refreshed_at: str
    status: str = "ok"       # ok | access_expired
    last_error: str = ""

    @property
    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TokenManager:
    """Reads/writes token metadata in the store and performs the refresh call."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        transport: Callable[[str, dict[str, str]], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.transport = transport or self._http_get

    def state(self) -> TokenState | None:
        raw = self.store.get_state(STATE_KEY)
        return TokenState(**raw) if raw else None

    def remember(self, token: str, expires_in_seconds: int) -> TokenState:
        """Record a freshly issued token's metadata (never the token value)."""
        state = TokenState(
            token_tail=token[-6:],
            expires_at=_iso(datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)),
            refreshed_at=_iso(datetime.now(timezone.utc)),
            status="ok",
        )
        self.store.set_state(STATE_KEY, asdict(state))
        return state

    def needs_refresh(self, now: datetime | None = None) -> bool:
        state = self.state()
        if state is None:
            return True
        now = now or datetime.now(timezone.utc)
        margin = timedelta(days=self.settings.token_refresh_margin_days)
        return state.expiry - now <= margin

    def refresh(self, now: datetime | None = None) -> TokenState:
        """Exchange the current long-lived token for a new one.

        On failure the connection is marked `access_expired` so the dashboard
        (and `GET /health`) can show it instead of silently dropping messages.
        """
        now = now or datetime.now(timezone.utc)

        if self.settings.demo_mode:
            log.info("demo mode: pretending Meta issued a fresh 60-day token")
            return self.remember("DEMOTOKEN000000", DEMO_LIFETIME_DAYS * 86400)

        url = f"{self.settings.graph_url}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.settings.phone_number_id,
            "client_secret": self.settings.app_secret,
            "fb_exchange_token": self.settings.access_token,
        }
        try:
            response = self.transport(url, params)
            if response.status_code >= 300:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            body = response.json()
            token = body.get("access_token")
            if not token:
                raise RuntimeError(f"no access_token in response: {str(body)[:200]}")
            state = self.remember(token, int(body.get("expires_in", DEMO_LIFETIME_DAYS * 86400)))
            log.info("token refreshed, valid until %s", state.expires_at)
            return state
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            return self.mark_expired(str(exc))

    def mark_expired(self, reason: str) -> TokenState:
        previous = self.state()
        state = TokenState(
            token_tail=previous.token_tail if previous else "",
            expires_at=previous.expires_at if previous else _iso(datetime.now(timezone.utc)),
            refreshed_at=previous.refreshed_at if previous else _iso(datetime.now(timezone.utc)),
            status="access_expired",
            last_error=reason[:300],
        )
        self.store.set_state(STATE_KEY, asdict(state))
        self.store.record_failure("token", "meta_access_token", reason)
        log.error("ACCESS EXPIRED - reconnect the WhatsApp account: %s", reason)
        return state

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        """What `GET /health` reports about the connection."""
        state = self.state()
        if state is None:
            return {"status": "unknown", "detail": "no token recorded yet"}
        now = now or datetime.now(timezone.utc)
        return {
            "status": state.status,
            "expires_at": state.expires_at,
            "days_left": round((state.expiry - now).total_seconds() / 86400, 1),
            "needs_refresh": self.needs_refresh(now),
            "last_error": state.last_error,
        }

    def _http_get(self, url: str, params: dict[str, str]):
        return requests.get(url, params=params, timeout=20)
