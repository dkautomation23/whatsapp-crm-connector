# whatsapp-crm-connector

[![CI](https://github.com/dkautomation23/whatsapp-crm-connector/actions/workflows/ci.yml/badge.svg)](https://github.com/dkautomation23/whatsapp-crm-connector/actions/workflows/ci.yml)

Receives WhatsApp Business Cloud webhooks and writes every conversation into a
CRM — **against the contact and against the company they belong to**. Sales
teams lose deals because a customer's WhatsApp thread lives on one salesperson's
phone: nobody else sees it, and the account record shows nothing. This connector
puts that thread where the whole team already looks.

Runs end to end with `python run_demo.py` — no Meta app, no CRM account, no
tokens.

---

## The flow

```
  WhatsApp customer
         |
         v
  Meta Cloud API  --POST-->  /webhook  (FastAPI)
                               |  1. verify X-Hub-Signature-256   -> 403 if forged
                               |  2. parse entry/changes/messages
                               |  3. enqueue
                               |  4. ACK 200 in milliseconds       <-- Meta stops retrying
                               v
                          asyncio queue
                               |
                     single worker (retries 3x, then logs)
                               |
              +----------------+-----------------+
              |                                  |
     known number?                        unknown number?
     match contact                        create contact,
              |                           flag needs_review
              +----------------+-----------------+
                               v
                  write timeline entry on the
                  CONTACT  *and*  its COMPANY
                               |
                               v
                    outbound reply / template
                    (24-hour window enforced)
```

Operational endpoints: `GET /health` (token status, queue counters, contacts
awaiting review), `GET /contacts/{id}/conversation`,
`GET /companies/{id}/conversation`.

## What it solves, concretely

| Failure this prevents | How |
| --- | --- |
| Forged webhooks | HMAC-SHA256 over the **raw** body, constant-time compare |
| Meta retrying a slow endpoint | verify → parse → enqueue → ACK; CRM work happens after the response |
| The same message stored twice | de-duplication by `message_id`, persisted in SQLite (survives restarts) |
| Duplicate contacts | phone numbers normalised (`+49 170 555 24 18` == `491705552418`) |
| Messages from unknown numbers being dropped | contact is created and flagged `needs_review`, message is stored either way |
| The account record showing no history | every entry is written with `company_id`, not only `contact_id` |
| "Message failed to send" after 24h silence | window checked before the call; outside it, only an approved template goes out |
| The integration going quiet after two months | long-lived token refreshed ahead of expiry; a failed refresh marks the connection `access_expired` in `/health` |
| Silent failures | every rejection, delivery failure and CRM error is written to a `failures` table |

## Run the demo

```bash
git clone https://github.com/dkautomation23/whatsapp-crm-connector.git
cd whatsapp-crm-connector
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_demo.py
```

Python 3.11. The demo seeds a company with two contacts, then posts the sample
webhooks from `samples/` — correctly signed — at the real endpoint.

### Real output (trimmed)

```console
CRM seeded: Nordwind Supplies GmbH with 2 contacts (Anna Weber, Jonas Keller)

1. Webhook subscription handshake (what Meta does once)
  correct verify token -> 200 1158201444
  wrong verify token   -> 403 verification failed

2. Forged webhook is rejected before anything is parsed
  bad signature -> 403 {'error': 'invalid signature'}

3. Inbound webhooks (ACKed immediately, processed in the queue)
  01_inbound_known_contact.json              -> 200 {'accepted': 1, 'statuses': 0}
  02_inbound_same_company_colleague.json     -> 200 {'accepted': 1, 'statuses': 0}
  03_inbound_unknown_number.json             -> 200 {'accepted': 1, 'statuses': 0}
  06_inbound_image_with_caption.json         -> 200 {'accepted': 1, 'statuses': 0}
  04_duplicate_delivery.json                 -> 200 {'accepted': 1, 'statuses': 0}
  05_status_failed.json                      -> 200 {'accepted': 0, 'statuses': 1}

4. What the worker did
{ "received": 5, "stored": 4, "duplicates": 1, "failed": 0, "statuses": 1 }

5. The point of the connector: the thread is on the COMPANY too
  company comp_nordwind: 3 messages from 2 contacts
   [2026-08-19T10:25:55Z] inbound  c_a76cdb948f: Hi, is the industrial gateway G2 still in stock? We need 12 units.
   [2026-08-19T10:40:55Z] inbound  c_74437f136b: Adding to Anna's request - please quote the annual service plan as
   [2026-08-19T11:10:55Z] inbound  c_a76cdb948f: [image] This is the socket we need to match

6. Unknown numbers are captured and flagged, never dropped
   c_f672c4dca9  +48511222333  'Marek'  needs_review=True  company=None

7. The 24-hour customer service window
   Anna: open=True, closes_at=2026-08-20T11:10:55Z, hours_left=23.2
   free-form send -> ok=True mode=text id=wamid.demo0001

   30 hours later -> window open=False
   free-form send blocked: 24h window closed (2026-08-20T10:55:55Z); an approved template name is required
   template send  -> ok=True mode=template template=shipping_update

8. Token lifecycle
   health: {"status": "ok", "expires_at": "2026-10-18T11:55:55Z", "days_left": 60.0, "needs_refresh": false}
   failed refresh -> status=access_expired, last_error='simulated: Meta returned 190 OAuthException'
   successful refresh -> status = ok

9. Nothing failed silently
   [2026-08-19T11:55:55Z] signature  invalid X-Hub-Signature-256
   [2026-08-19T11:55:55Z] delivery   Re-engagement message outside the 24 hour window
   [2026-08-19T11:55:55Z] token      simulated: Meta returned 190 OAuthException
```

## Going live

1. Fill in `.env` (see `.env.example`): app secret, verify token, long-lived
   access token, phone number id. Set `DEMO_MODE=false`.
2. Serve the app over HTTPS: `uvicorn wa_connector.app:app --port 8000`.
3. In the Meta app dashboard set the callback URL to `https://…/webhook` and
   paste the same verify token; subscribe to the `messages` field.
4. Replace `LocalCrm` in `wa_connector/crm.py` with an adapter for the real CRM
   — four methods (`find_contact`, `create_contact`, `log_conversation`,
   `company_of`). Everything else stays as it is.
5. Schedule `TokenManager.refresh()` daily (cron, APScheduler, a Cloud
   Scheduler job) and alert on `status == "access_expired"`.

`GET /health` reports `missing_env_for_live`, so a half-configured deployment
says so instead of failing on the first customer message.

## Project layout

```
wa_connector/
  app.py         FastAPI: handshake, webhook, health, conversation views
  signature.py   X-Hub-Signature-256 verification + subscription handshake
  models.py      webhook envelope -> flat InboundMessage / StatusUpdate
  worker.py      asyncio queue + single consumer with retries
  crm.py         contact matching, review flags, CRM adapter protocol
  store.py       SQLite: de-duplication, contacts, companies, timeline, failures
  outbound.py    24-hour window, templates, retry/backoff
  tokens.py      long-lived token refresh and the access_expired flag
samples/         six real-shaped webhook payloads (text, image, duplicate, status)
tests/           42 tests
run_demo.py      the whole flow, offline
```

## Tests

```bash
pytest
```

```console
..........................................                               [100%]
42 passed in 4.30s
```

Covers signature verification (including tampered bodies and an empty secret),
envelope parsing of unknown message types, contact matching across phone
formats, de-duplication on redelivery, the company timeline, the 24-hour window
in both directions, retry behaviour on 503 / connection errors versus a 401 that
is not retried, token refresh and expiry marking, and the HTTP endpoints end to
end. No network calls.

## License

MIT — see [LICENSE](LICENSE).
