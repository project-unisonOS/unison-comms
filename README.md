# unison-comms

Communications service for UnisonOS. Provides intent-centric comms endpoints (`comms.check`, `comms.summarize`, `comms.reply`, `comms.compose`) behind a normalized message shape, ready to feed the orchestrator and Operating Surface.

## Status
Active bounded service slice. Core comms endpoints remain simple, and Gmail now has a real onboarding/readiness contract surface with draft-first compose behavior.

Phase 5 adds the Telegram Channel Gateway without removing the Gmail adapter. See [the Telegram channel guide](docs/telegram-channel.md) for pairing, disclosure, privacy, recovery, deployment, and credential-free conformance details.

## Run
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
python src/run.py
```

Security note: `unison-comms` defaults to loopback-only binding. To bind to `0.0.0.0` for devstack/container networking, set:
```bash
export COMMS_HOST=0.0.0.0
export COMMS_UNSAFE_ALLOW_NONLOCAL=true
```

## Testing
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OTEL_SDK_DISABLED=true python -m pytest
```

## Endpoints
- `GET /health`
- `GET /readyz` — returns service readiness plus email provider readiness/onboarding state
- `POST /comms/check` — returns normalized messages + dashboard-friendly cards
- `POST /comms/summarize` — returns a summary and summary cards
- `POST /comms/reply` — validates identifiers, returns confirmation
- `POST /comms/compose` — validates recipients/subject; Gmail currently supports draft-first bounded behavior
- Email onboarding endpoints:
  - `GET /comms/onboarding/email` — current onboarding/readiness state
  - `POST /comms/onboarding/email/bootstrap` — persist bounded local Gmail bootstrap credentials in the encrypted local store
  - `POST /comms/onboarding/email/verify` — bounded verification of current Gmail configuration
  - `POST /comms/onboarding/email/reset` — bounded disconnect/reset contract
  - `GET /comms/onboarding/email/oauth` — OAuth-ready device-flow contract, not yet implemented
- Meeting stubs:
  - `POST /comms/join_meeting` — returns join card
  - `POST /comms/prepare_meeting` — returns prep/agenda card
  - `POST /comms/debrief_meeting` — returns debrief/summary card

## Email adapters

- Default: in-memory stub (no external network, good for local dev/tests).
- Optional Gmail (IMAP/SMTP) when configured via env:
  - `COMMS_EMAIL_PROVIDER=gmail`
  - `GMAIL_USERNAME=<your gmail address>`
  - `GMAIL_APP_PASSWORD=<app password>` (generated after enabling 2FA; see docs/email-onboarding.md)
  - Optional: `GMAIL_IMAP_HOST`, `GMAIL_SMTP_HOST`
  - Optional: `COMMS_GMAIL_DRAFT_ONLY=true` (default) to keep compose in bounded draft-first mode
  - Optional: `COMMS_GMAIL_STORE_PATH=/tmp/unison-comms-gmail.json`
  - Optional: `COMMS_GMAIL_KEY=<base64 Fernet key>`
- If Gmail env vars are absent, the service can also resolve credentials from the local encrypted bootstrap store.
- If Gmail config is missing or invalid, the service falls back to the in-memory stub for adapter resolution.
- Unison-to-Unison channel: handled locally via an in-memory adapter (`channel: "unison"`), storing messages on-device. You can override storage path/key:
  - `COMMS_UNISON_STORE_PATH=/tmp/unison-comms-unison.json`
  - `COMMS_UNISON_KEY=<optional base64 Fernet key>` (if unset, stored plaintext locally)

### Adapter interface (for adding more providers)

- Implement the `EmailAdapter` protocol (see `src/main.py`):
  - `fetch_messages(channel: str = "email") -> list[dict]` returning normalized messages.
  - `send_reply(person_id, thread_id, message_id, body, recipients=None) -> dict`.
  - `send_compose(person_id, channel, recipients, subject, body) -> dict`.
- Update `_resolve_adapter()` to select your adapter based on an env flag (e.g., `COMMS_EMAIL_PROVIDER=myprovider`).
- Keep provider-specific secrets in env vars and ensure they remain on-device.
- Normalize output to the common message shape and set `context_tags` (e.g., `["comms", "email", "p1"]`).

## Onboarding flows

- Developer (configure Gmail):
  - Enable 2FA in your Google account.
  - Create an App Password (choose “Mail” → “Other/Custom Name”).
  - Export as env vars: `COMMS_EMAIL_PROVIDER=gmail`, `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`.
  - Use `GET /comms/onboarding/email` to inspect readiness.
  - Use `POST /comms/onboarding/email/bootstrap` to persist local Gmail credentials without relying on env-only setup.
  - Use `POST /comms/onboarding/email/verify` to exercise the current Gmail config.
  - Use `POST /comms/onboarding/email/reset` for the bounded disconnect/reset contract.
- Person (conversational flow, edge-first):
  - Companion asks for email provider (“Gmail”) and address.
  - Explains that tokens/app passwords stay local and never leave the device.
  - Uses the onboarding endpoints to expose current state and next action.
  - May offer the OAuth-ready production path via `GET /comms/onboarding/email/oauth`, which currently advertises intent but does not yet perform the device-code exchange.

## Docs

Full docs at https://project-unisonos.github.io
