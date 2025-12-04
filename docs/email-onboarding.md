# Email Onboarding (Gmail example)

This guide covers two perspectives:
- Developers: how to configure the communications service to talk to Gmail (edge-only).
- People using a deployed UnisonOS: what the conversational onboarding flow should look like.

## Developer setup (Gmail with app password)

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
4. **Run** `unison-comms` and call `POST /comms/check` to verify:
   - Returns normalized messages with `context_tags` like `["comms","email","p0"]`.
5. **Privacy/edge**: credentials remain on the device; nothing is written to repos.

## Person onboarding flow (conversational)

Goal: connect email to UnisonOS without exposing secrets or breaking the “edge-first” promise.

1. **Intent**: Person says “connect my Gmail” (or similar).
2. **Companion explains**:
   - Tokens/app passwords stay on-device and are encrypted.
   - You can disconnect at any time; no inbox is uploaded to cloud services.
3. **Collect account info**:
   - Ask for email address.
   - Offer guidance to generate an app password (or OAuth if implemented later).
4. **Capture secret (one-time)**:
   - Prompt to paste the app password (no logging; store encrypted locally).
   - Confirm storage succeeded.
5. **Verify**:
   - Run `comms.check` once; if messages are found, present a “Messages to respond to” card on the dashboard.
   - If no messages are available, return a success acknowledgment and suggest trying again later.
6. **Next actions**:
   - Offer summaries (`comms.summarize`), replies (`comms.reply`), or compose (`comms.compose`) through voice/chat.
   - Remind how to disconnect or rotate credentials.

### UX notes
- Keep prompts short and clear; never echo secrets back.
- Emphasize local-only handling and explicit consent.
- Handle failure paths gracefully (bad password, offline) with actionable guidance.
