# Security Policy

## Supported Versions

There are no tagged releases yet. Only the latest commit on `main` is supported with security fixes.

## Reporting a Vulnerability

Report privately using GitHub's Private Vulnerability Reporting: open this repository's Security tab and select "Report a vulnerability". If that option is not available to you, email hello@dkautomation.dev instead.

Do not open a public issue for a suspected vulnerability.

You will get a first response within 3 business days.

A good report includes:

- Steps to reproduce the issue
- The affected file(s) or endpoint
- The impact — what an attacker can do as a result

## Scope

### Treated as a vulnerability here

- Forging or otherwise bypassing the `X-Hub-Signature-256` HMAC check performed by `verify_signature()` in `wa_connector/signature.py`.
- Any way to make the webhook endpoint (`POST /webhook` in `wa_connector/app.py`) accept and process a payload that is unsigned, malformed, or signed with the wrong key.
- The Meta app secret (`META_APP_SECRET`) or the WhatsApp access token (`META_ACCESS_TOKEN`) leaking into logs, HTTP error responses, or the `state.sqlite3` store in recoverable plaintext — for example through `TokenManager`'s stored `last_error` field (`wa_connector/tokens.py`) or `Store.record_failure()` (`wa_connector/store.py`).
- Any bypass of the 24-hour messaging-window enforcement in `wa_connector/outbound.py` (`window_state()` / `Sender.send()`) that lets a free-form text message go out after the window — driven by `Store.last_inbound_at()` — should have closed it.
- Any bypass of the access-token lifecycle enforcement in `wa_connector/tokens.py` (`TokenManager.needs_refresh()` / `refresh()` / `mark_expired()`) — for example, a connection marked `access_expired` being treated as healthy.

### Not treated as a vulnerability here

- Anything that requires already possessing the Meta App Secret. Being able to sign a valid request with it is the trust boundary this connector relies on, not a bypass of it.
- Load, rate-limit, or resource-exhaustion behavior against your own local demo instance (`DEMO_MODE=true`).
