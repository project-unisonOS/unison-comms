# Email Onboarding (Gmail example)

This guide covers two perspectives:
- Developers: how to configure the communications service to talk to Gmail (edge-only).
- People using a deployed UnisonOS: what the conversational onboarding flow should look like.

## Developer setup (Gmail with app password)

Note: App passwords are a developer convenience and are not the production onboarding path.
Production connector onboarding should use OAuth via `unison-capability` (device authorization grant) with secrets stored outside manifests and referenced by secret handles.

1. **Enable 2FA** on the Gmail account you want to use.
2. **Create an App Password**:
   - Google Account → Security → “App passwords”.
   - App: Mail; Device: Other/Custom (e.g., “UnisonOS”).
   - Copy the 16-character app password.
3. **Set environment variables** (local only, never checked into git):
   ```bash
   export COMMS_EMAIL_PROVIDER=gmail
   export GMAIL_USERNAME="your-address@gmail.com"
   export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
   # optional overrides:
   # export GMAIL_IMAP_HOST=imap.gmail.com
   # export GMAIL_SMTP_HOST=smtp.gmail.com
   ```
4. **Inspect onboarding/readiness** with `GET /comms/onboarding/email`:
   - Returns bounded readiness and onboarding state such as `not_configured`, `missing_secret`, or `configured`.
5. **Verify** with `POST /comms/onboarding/email/verify`:
   - Exercises the current Gmail configuration and reports `verified`, `needs_configuration`, or `verification_failed`.
6. **Compose behavior**:
   - `POST /comms/compose` uses draft-first Gmail behavior by default (`COMMS_GMAIL_DRAFT_ONLY=true`) and returns a normalized `draft` result instead of falsely reporting `sent` on failure.
7. **Disconnect/reset** with `POST /comms/onboarding/email/reset`:
   - Returns a bounded reset contract and tells the caller which local Gmail env keys should be cleared to fully disconnect.
8. **Privacy/edge**: credentials remain on the device; nothing is written to repos.

## Person onboarding flow (conversational)

Goal: connect email to UnisonOS without exposing secrets or breaking the “edge-first” promise.

1. **Intent**: Person says “connect my Gmail” (or similar).
2. **Companion explains**:
   - Tokens/app passwords stay on-device and are encrypted.
   - You can disconnect at any time; no inbox is uploaded to cloud services.
3. **Collect account info**:
   - Ask for email address.
   - Offer guidance to generate an app password, or show the OAuth-ready production path.
4. **Expose onboarding state**:
   - `GET /comms/onboarding/email` shows the current state and next action.
5. **Capture secret (current bounded path)**:
   - App-password handling is still external to this repo; once local config is present, `POST /comms/onboarding/email/verify` can exercise it.
6. **OAuth-ready production contract**:
   - `GET /comms/onboarding/email/oauth` advertises the intended device authorization flow through `unison-capability`, but does not yet execute it.
7. **Next actions**:
   - Offer summaries (`comms.summarize`), replies (`comms.reply`), or draft-first compose (`comms.compose`) through voice/chat.
   - Use `POST /comms/onboarding/email/reset` to surface disconnect/reset guidance.

### Current implemented endpoints
- `GET /comms/onboarding/email`
- `POST /comms/onboarding/email/verify`
- `POST /comms/onboarding/email/reset`
- `GET /comms/onboarding/email/oauth`

### UX notes
- Keep prompts short and clear; never echo secrets back.
- Emphasize local-only handling and explicit consent.
- Handle failure paths gracefully (bad password, offline) with actionable guidance.
- Do not imply OAuth or secret-store persistence is complete until those integrations actually exist.
