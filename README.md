# unison-comms

Communications service for UnisonOS. Provides intent-centric comms endpoints (`comms.check`, `comms.summarize`, `comms.reply`, `comms.compose`) behind a normalized message shape, ready to feed the orchestrator and Operating Surface.

## Status
New service skeleton (active) — stubbed HTTP API and health checks; adapters to be implemented next.

## Run
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

## Testing
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OTEL_SDK_DISABLED=true python -m pytest
```

## Endpoints (stubbed)
- `GET /health`, `GET /readyz`
- `POST /comms/check` — returns normalized messages + dashboard-friendly cards via in-memory email adapter stub
- `POST /comms/summarize` — returns a summary and summary cards
- `POST /comms/reply` — validates identifiers, returns confirmation
- `POST /comms/compose` — validates recipients/subject, returns confirmation and stores the composed message in memory

## Email adapters

- Default: in-memory stub (no external network, good for local dev/tests).
- Optional Gmail (IMAP/SMTP) when configured via env:
  - `COMMS_EMAIL_PROVIDER=gmail`
  - `GMAIL_USERNAME=<your gmail address>`
  - `GMAIL_APP_PASSWORD=<app password>` (generated after enabling 2FA; see docs/email-onboarding.md)
  - Optional: `GMAIL_IMAP_HOST`, `GMAIL_SMTP_HOST`
- If Gmail config is missing or invalid, the service falls back to the in-memory stub.
- Unison-to-Unison channel: handled locally via an in-memory adapter (`channel: "unison"`), storing messages on-device.

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
  - Run the service and hit `/comms/check` to see live normalized messages (if available).
- Person (conversational flow, edge-first):
  - Companion asks for email provider (“Gmail”) and address.
  - Explains that tokens/app passwords stay local and never leave the device.
  - Prompts for a one-time app password or OAuth token; stores it encrypted on-device.
  - Confirms by running a `comms.check` and presenting “Messages to respond to” cards on the dashboard.

## Docs

Full docs at https://project-unisonos.github.io
