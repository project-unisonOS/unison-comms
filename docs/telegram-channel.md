# Telegram remote text channel

Phase 5 uses the Telegram Bot API `getUpdates` long-poll operation as its first reference remote-text provider. The worker initiates every network connection; it does not require a webhook or a public listener on the appliance.

## Assurance and privacy boundary

Telegram is a low-assurance convenience channel, not a trusted UnisonOS surface. Only one-to-one private bot chats are accepted. Telegram can process message content and account, chat, network, and delivery metadata. Bot chats are not end-to-end encrypted to the UnisonOS node, and pending Bot API updates may remain at Telegram for up to 24 hours. Never send passwords, recovery codes, financial details, medical records, or other secrets through the bot.

Each person supplies and owns a separate bot credential. UnisonOS encrypts it in that person's credential namespace. Provider accounts, threads, drafts, delivery state, and audit summaries cannot be read or reassigned by another household member.

Official provider references reviewed July 21, 2026:

- Bot API and `getUpdates`: https://core.telegram.org/bots/api
- Bot platform security model: https://core.telegram.org/bots
- Telegram privacy policy: https://telegram.org/privacy

## Pair locally, then message remotely

1. Create a bot with BotFather and copy its token on your trusted local device.
2. In UnisonOS, open **Remote assistant**, review the disclosure, and store the token for your own assistant.
3. Authenticate locally with a passkey or equivalent high-assurance method.
4. Request a one-use pairing code. It expires after ten minutes.
5. Open a private chat with your bot and send `/pair CODE`.
6. Return to UnisonOS and run the connection check. A generic denial is returned for invalid, expired, reused, or reassigned codes so the remote channel does not reveal private account state.

Bots cannot initiate a conversation, so the person must start the private chat. Group, channel, attachment-only, delayed, duplicate, replayed, out-of-order, and unbound messages fail closed.

## Step-up and delivery semantics

Safe text requests are normalized with person, assistant, provider, capability, privacy, nonce, delivery, and assurance fields. Requests involving sensitive data, account recovery, consequential external action, or ambiguity return **Continue on your trusted local device** and do not execute remotely.

Outbound content is draft-first. A draft expires after ten minutes and is sent only after the same person confirms it on a high-assurance local surface. Every draft supports cancellation. Delivery and audit records retain identifiers, hashes, timestamps, and outcomes—not plaintext message content.

## Revoke, rotate, and recover

- Revoke the channel from the trusted local **Remote assistant** control. Revocation clears the encrypted token and immediately rejects further polling.
- If the token may be stolen, revoke it with BotFather, revoke the UnisonOS provider account, create a new per-person bot token, and pair again.
- A Telegram outage leaves the last committed update cursor unchanged. The worker retries with bounded exponential backoff and resumes from the next update after service returns.
- A reinstall or disconnect never silently reuses a prior binding. Re-register the credential and repeat strong local pairing.

## Deployment

Required secret-file-capable settings are `CHANNEL_GATEWAY_ROOT_KEY` and `AUTH_CHANNEL_WORKLOAD_SECRET`. Register `AUTH_CHANNEL_WORKLOAD_ID` in auth with audience `auth` and only the `channel:bind` scope. The gateway obtains short-lived workload tokens and never uses a person token for internal binding. Persist `CHANNEL_GATEWAY_DB` on encrypted local storage. One worker discovers all active per-person provider accounts without sharing their credentials or state.

Run `python -m run_telegram_worker`. Do not publish the comms or auth container ports to a WAN interface. The worker needs outbound HTTPS to `api.telegram.org`; no inbound firewall rule is required.

## Credential-free conformance

`tests/test_channel_gateway.py` uses `FakeTelegramProvider` and two local people. It covers pairing, one-use challenges, reassignment defense, stolen subjects, replay, duplicate, delay, group rejection, rate limiting, provider outage/reconnect, low-assurance step-up, draft confirmation, isolation, and revocation without contacting Telegram or storing a real credential.
