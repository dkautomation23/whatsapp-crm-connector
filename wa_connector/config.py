"""Configuration. Every credential comes from the environment - see .env.example.

DEMO_MODE is the switch that makes this repository runnable by anyone: no Meta
app, no CRM account, no tokens. The same code paths run, only the outbound HTTP
calls are replaced by a recorder.

The environment is read inside `get_settings()`, not in the class body, so a
test (or a reload) can change it without re-importing the module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    # --- Meta WhatsApp Business Cloud API ---------------------------------
    # Verifies X-Hub-Signature-256 on every inbound webhook.
    app_secret: str = ""
    # Echoed back during the one-time webhook subscription handshake.
    verify_token: str = ""
    # Long-lived access token; refreshed by tokens.py before it expires.
    access_token: str = ""
    phone_number_id: str = ""
    graph_version: str = "v21.0"

    # --- CRM --------------------------------------------------------------
    crm_base_url: str = ""
    crm_api_key: str = ""

    # --- behaviour --------------------------------------------------------
    demo_mode: bool = True
    state_path: str = "state.sqlite3"
    max_retries: int = 4
    retry_base_seconds: float = 0.5
    # Refresh the long-lived token once it is this close to expiry.
    token_refresh_margin_days: int = 7

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"

    def missing_for_live(self) -> list[str]:
        """Which variables still have to be filled in before going live."""
        required = {
            "META_APP_SECRET": self.app_secret,
            "META_VERIFY_TOKEN": self.verify_token,
            "META_ACCESS_TOKEN": self.access_token,
            "WHATSAPP_PHONE_NUMBER_ID": self.phone_number_id,
        }
        return [name for name, value in required.items() if not value]


def load_settings() -> Settings:
    """Build Settings from the current environment."""
    return Settings(
        app_secret=os.getenv("META_APP_SECRET", ""),
        verify_token=os.getenv("META_VERIFY_TOKEN", ""),
        access_token=os.getenv("META_ACCESS_TOKEN", ""),
        phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        graph_version=os.getenv("META_GRAPH_VERSION", "v21.0"),
        crm_base_url=os.getenv("CRM_BASE_URL", ""),
        crm_api_key=os.getenv("CRM_API_KEY", ""),
        demo_mode=_flag("DEMO_MODE", True),
        state_path=os.getenv("STATE_PATH", "state.sqlite3"),
        max_retries=int(os.getenv("MAX_RETRIES", "4")),
        retry_base_seconds=float(os.getenv("RETRY_BASE_SECONDS", "0.5")),
        token_refresh_margin_days=int(os.getenv("TOKEN_REFRESH_MARGIN_DAYS", "7")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
