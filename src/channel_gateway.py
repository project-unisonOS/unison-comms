"""Private, low-assurance remote text gateway for Phase 5."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from cryptography.fernet import Fernet, InvalidToken
from unison_common.channel import (
    ChannelAssurance,
    ChannelCapabilities,
    ChannelDirection,
    DeliveryState,
    NormalizedChannelEnvelope,
    ProviderPrivacyMetadata,
    SemanticChannelOutcome,
)


TELEGRAM_PRIVACY = ProviderPrivacyMetadata(
    provider="telegram",
    provider_reads_content=True,
    provider_retains_content=True,
    retention_summary="Telegram relays Bot API traffic and may retain pending updates for up to 24 hours.",
    provider_receives_identifiers=("Telegram user ID", "chat ID", "bot ID", "IP metadata"),
    end_to_end_encrypted_to_node=False,
    monetizes_personal_data=None,
    policy_url="https://telegram.org/privacy",
    reviewed_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
)
TELEGRAM_CAPABILITIES = ChannelCapabilities(text=True, maximum_text_length=4096)
SENSITIVE_TERMS = ("password", "bank", "payment", "wire", "unlock", "medical record", "social security")
RECOVERY_TERMS = ("recover account", "reset password", "recovery code", "lost passkey")


class ChannelDenied(RuntimeError):
    pass


class ProviderUnavailable(RuntimeError):
    pass


class RateLimitExceeded(RuntimeError):
    pass


class BindingAuthority(Protocol):
    def complete_channel_pairing_by_code(self, **kwargs: Any) -> dict[str, Any]: ...
    def resolve_channel_binding(self, **kwargs: Any) -> dict[str, Any] | None: ...
    def revoke_provider_account_bindings(self, **kwargs: Any) -> int: ...


class AuthBindingClient:
    """Narrow workload-authenticated client for the auth-owned binding authority."""

    def __init__(self, base_url: str, client_id: str, client_secret: str, *, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""
        self._token_expires_at = 0.0
        self._client = client or httpx.Client(timeout=5)

    def _access_token(self) -> str:
        if self._token and self._token_expires_at > time.time() + 30:
            return self._token
        try:
            response = self._client.post(
                f"{self._base_url}/token",
                data={
                    "username": self._client_id,
                    "password": self._client_secret,
                    "grant_type": "client_credentials",
                    "audience": "auth",
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable("binding authority authentication is unavailable") from exc
        self._token = str(body["access_token"])
        self._token_expires_at = time.time() + int(body.get("expires_in", 300))
        return self._token

    def _request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._client.post(
                f"{self._base_url}/internal/channels/{operation}",
                json=payload,
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("binding authority is unavailable") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ChannelDenied("binding authority denied the request")
        return response.json()

    def complete_channel_pairing_by_code(self, **kwargs: Any) -> dict[str, Any]:
        result = self._request("complete", kwargs)
        if result is None:
            raise ChannelDenied("pairing is unavailable")
        return result

    def resolve_channel_binding(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._request("resolve", kwargs)

    def revoke_provider_account_bindings(self, **kwargs: Any) -> int:
        result = self._request("revoke-account", kwargs)
        return int((result or {}).get("binding_count", 0))


class TelegramProvider(Protocol):
    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]: ...
    def send_message(self, *, chat_id: str, text: str) -> str: ...


class TelegramLongPollAdapter:
    """Outbound-only Bot API client. It opens no inbound network listener."""

    def __init__(self, token: str, *, client: httpx.Client | None = None):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.Client(timeout=45)

    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"{self._base_url}/{method}", json=payload)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable("Telegram provider is unavailable") from exc
        if not result.get("ok"):
            raise ProviderUnavailable("Telegram provider rejected the request")
        return result.get("result")

    def get_updates(self, *, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        result = self._post(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
        )
        return result if isinstance(result, list) else []

    def send_message(self, *, chat_id: str, text: str) -> str:
        result = self._post("sendMessage", {"chat_id": chat_id, "text": text})
        return str((result or {}).get("message_id", "unknown"))


class FakeTelegramProvider:
    def __init__(self):
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, str]] = []
        self.available = True

    def get_updates(self, *, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        if not self.available:
            raise ProviderUnavailable("simulated outage")
        return [item for item in self.updates if int(item["update_id"]) >= offset]

    def send_message(self, *, chat_id: str, text: str) -> str:
        if not self.available:
            raise ProviderUnavailable("simulated outage")
        message_id = str(len(self.sent) + 1)
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": message_id})
        return message_id


def _iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class PollResult:
    status: str
    update_id: int | None = None
    envelope: NormalizedChannelEnvelope | None = None
    outcome: SemanticChannelOutcome | None = None


class ChannelGateway:
    def __init__(
        self,
        database_path: str,
        root_key: str,
        binding_authority: BindingAuthority,
        provider_factory: Callable[[str], TelegramProvider] = TelegramLongPollAdapter,
        *,
        now: Callable[[], float] = time.time,
        replay_window_seconds: int = 300,
        rate_limit_per_minute: int = 12,
    ):
        if len(root_key) < 24:
            raise ValueError("channel root key must contain at least 24 characters")
        self.database_path = str(Path(database_path))
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._root_key = root_key.encode()
        self._binding_authority = binding_authority
        self._provider_factory = provider_factory
        self._now = now
        self.replay_window_seconds = replay_window_seconds
        self.rate_limit_per_minute = rate_limit_per_minute
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_accounts (
                  provider_account_id TEXT PRIMARY KEY, person_id TEXT NOT NULL,
                  credential_namespace TEXT NOT NULL UNIQUE, token_cipher TEXT NOT NULL,
                  bot_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                  last_update_id INTEGER NOT NULL DEFAULT -1, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_events (
                  provider_account_id TEXT NOT NULL, update_id INTEGER NOT NULL,
                  person_id TEXT, event_hash TEXT NOT NULL, disposition TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,
                  PRIMARY KEY(provider_account_id, update_id)
                );
                CREATE TABLE IF NOT EXISTS channel_drafts (
                  draft_id TEXT PRIMARY KEY, provider_account_id TEXT NOT NULL,
                  person_id TEXT NOT NULL, chat_id_cipher TEXT NOT NULL, text_cipher TEXT NOT NULL,
                  purpose TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL,
                  created_at TEXT NOT NULL, provider_message_id TEXT
                );
                """
            )

    def _fernet(self, namespace: str) -> Fernet:
        key = hmac.new(self._root_key, f"channel\0{namespace}".encode(), hashlib.sha256).digest()
        return Fernet(urlsafe_b64encode(key))

    def _encrypt(self, namespace: str, value: str) -> str:
        return self._fernet(namespace).encrypt(value.encode()).decode()

    def _decrypt(self, namespace: str, value: str) -> str:
        try:
            return self._fernet(namespace).decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ChannelDenied("provider account is unavailable") from exc

    def register_telegram_account(
        self, *, person_id: str, provider_account_id: str, token: str, bot_id: str
    ) -> dict[str, str]:
        namespace = f"telegram:{person_id}:{provider_account_id}"
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT person_id FROM provider_accounts WHERE provider_account_id=?",
                (provider_account_id,),
            ).fetchone()
            if existing and existing["person_id"] != person_id:
                raise ChannelDenied("provider account is unavailable")
            connection.execute(
                """
                INSERT INTO provider_accounts VALUES (?, ?, ?, ?, ?, 'active', -1, ?)
                ON CONFLICT(provider_account_id) DO UPDATE SET
                  token_cipher=excluded.token_cipher, bot_id=excluded.bot_id, status='active'
                """,
                (provider_account_id, person_id, namespace, self._encrypt(namespace, token), bot_id, _iso(self._now())),
            )
        return {"provider_account_id": provider_account_id, "person_id": person_id, "status": "active"}

    def revoke_account(self, *, person_id: str, provider_account_id: str) -> bool:
        with self._connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM provider_accounts WHERE provider_account_id=? AND person_id=? AND status='active'",
                (provider_account_id, person_id),
            ).fetchone()
        if owned is None:
            return False
        self._binding_authority.revoke_provider_account_bindings(
            person_id=person_id, provider="telegram", provider_account_id=provider_account_id
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE provider_accounts SET status='revoked', token_cipher='' WHERE provider_account_id=? AND person_id=? AND status='active'",
                (provider_account_id, person_id),
            )
        return cursor.rowcount == 1

    def _account(self, provider_account_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_accounts WHERE provider_account_id=? AND status='active'",
                (provider_account_id,),
            ).fetchone()
        if row is None:
            raise ChannelDenied("provider account is unavailable")
        return row

    def account_owner(self, provider_account_id: str) -> str:
        return str(self._account(provider_account_id)["person_id"])

    def active_account_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT provider_account_id FROM provider_accounts WHERE status='active' ORDER BY provider_account_id"
            ).fetchall()
        return [str(row["provider_account_id"]) for row in rows]

    def _record(self, account: sqlite3.Row, update_id: int, person_id: str | None, disposition: str, raw: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO channel_events VALUES (?, ?, ?, ?, ?, ?)",
                (account["provider_account_id"], update_id, person_id, _hash(json.dumps(raw, sort_keys=True)), disposition, _iso(self._now())),
            )
            connection.execute(
                "UPDATE provider_accounts SET last_update_id=MAX(last_update_id, ?) WHERE provider_account_id=?",
                (update_id, account["provider_account_id"]),
            )

    def _rate_limited(self, account_id: str) -> bool:
        cutoff = datetime.fromtimestamp(self._now() - 60, timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM channel_events WHERE provider_account_id=? AND recorded_at>=?",
                (account_id, cutoff),
            ).fetchone()["count"]
        return int(count) >= self.rate_limit_per_minute

    def poll(self, provider_account_id: str) -> list[PollResult]:
        account = self._account(provider_account_id)
        token = self._decrypt(account["credential_namespace"], account["token_cipher"])
        provider = self._provider_factory(token)
        updates = provider.get_updates(offset=int(account["last_update_id"]) + 1, timeout=30)
        results: list[PollResult] = []
        for update in sorted(updates, key=lambda item: int(item.get("update_id", -1))):
            update_id = int(update.get("update_id", -1))
            with self._connect() as connection:
                already_seen = connection.execute(
                    "SELECT 1 FROM channel_events WHERE provider_account_id=? AND update_id=?",
                    (provider_account_id, update_id),
                ).fetchone()
            if update_id <= int(account["last_update_id"]) or already_seen:
                results.append(PollResult("replay-rejected", update_id))
                continue
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            subject, chat_id = str(sender.get("id", "")), str(chat.get("id", ""))
            text = message.get("text") if isinstance(message.get("text"), str) else ""
            occurred = float(message.get("date", 0))
            if chat.get("type") != "private" or not subject or not text:
                self._record(account, update_id, None, "unsupported-or-non-private", update)
                results.append(PollResult("unsupported-or-non-private", update_id))
                continue
            if occurred < self._now() - self.replay_window_seconds:
                self._record(account, update_id, None, "delayed-rejected", update)
                results.append(PollResult("delayed-rejected", update_id))
                continue
            if self._rate_limited(provider_account_id):
                self._record(account, update_id, None, "rate-limited", update)
                results.append(PollResult("rate-limited", update_id))
                continue
            if text.startswith("/pair "):
                code = text.removeprefix("/pair ").strip()
                try:
                    binding = self._binding_authority.complete_channel_pairing_by_code(
                        pairing_code=code,
                        provider="telegram",
                        provider_account_id=provider_account_id,
                        external_subject=subject,
                    )
                    if binding["person_id"] != account["person_id"]:
                        raise ChannelDenied("pairing is unavailable")
                    disposition = "paired"
                except RuntimeError:
                    disposition = "pairing-denied"
                self._record(account, update_id, account["person_id"], disposition, update)
                results.append(PollResult(disposition, update_id))
                continue
            binding = self._binding_authority.resolve_channel_binding(
                provider="telegram", provider_account_id=provider_account_id, external_subject=subject
            )
            if binding is None or binding["person_id"] != account["person_id"]:
                self._record(account, update_id, None, "unbound-denied", update)
                results.append(PollResult("unbound-denied", update_id))
                continue
            lowered = text.lower()
            sensitive = any(term in lowered for term in SENSITIVE_TERMS)
            recovery = any(term in lowered for term in RECOVERY_TERMS)
            step_up = sensitive or recovery
            envelope = NormalizedChannelEnvelope(
                event_id=f"evt_{uuid.uuid4().hex}",
                provider="telegram",
                provider_account_id=provider_account_id,
                direction=ChannelDirection.INBOUND,
                external_subject=subject,
                external_thread_id=chat_id,
                provider_event_id=str(update_id),
                occurred_at=datetime.fromtimestamp(occurred, timezone.utc),
                nonce=secrets.token_urlsafe(24),
                text=text,
                assurance=ChannelAssurance.LOW,
                capabilities=TELEGRAM_CAPABILITIES,
                privacy=TELEGRAM_PRIVACY,
                delivery_state=DeliveryState.RECEIVED,
                bound_person_id=binding["person_id"],
                bound_assistant_instance_id=binding["assistant_instance_id"],
                sensitive_action_requested=sensitive,
                recovery_action_requested=recovery,
                step_up_required=step_up,
            )
            outcome = SemanticChannelOutcome(
                status="step-up-required" if step_up else "accepted",
                concise_text="Continue on your trusted local device." if step_up else "Request received.",
                simplified_text="Use your home device to confirm this safely." if step_up else "I received your message.",
                privacy_notice="Telegram can process this message; do not send secrets.",
                confirmation_required=step_up,
                step_up_required=step_up,
                recovery_guidance="Open UnisonOS locally or revoke this channel from a trusted device.",
            )
            disposition = "step-up-required" if step_up else "accepted"
            self._record(account, update_id, binding["person_id"], disposition, update)
            results.append(PollResult(disposition, update_id, envelope, outcome))
        return results

    def create_outbound_draft(
        self, *, person_id: str, provider_account_id: str, chat_id: str, text: str, purpose: str
    ) -> dict[str, Any]:
        account = self._account(provider_account_id)
        if account["person_id"] != person_id:
            raise ChannelDenied("draft is unavailable")
        draft_id = f"draft_{uuid.uuid4().hex}"
        expires_at = self._now() + 600
        namespace = account["credential_namespace"]
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO channel_drafts VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, NULL)",
                (draft_id, provider_account_id, person_id, self._encrypt(namespace, chat_id), self._encrypt(namespace, text), purpose, expires_at, _iso(self._now())),
            )
        return {"draft_id": draft_id, "status": "draft", "expires_at": _iso(expires_at), "confirmation_required": True}

    def confirm_outbound_draft(self, *, person_id: str, draft_id: str, assurance: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed or assurance not in {"high", "hardware", "passkey"}:
            raise ChannelDenied("strong local confirmation is required")
        with self._connect() as connection:
            draft = connection.execute(
                "SELECT d.*, a.credential_namespace, a.token_cipher, a.status AS account_status FROM channel_drafts d JOIN provider_accounts a USING(provider_account_id) WHERE draft_id=? AND d.person_id=? AND d.status='draft'",
                (draft_id, person_id),
            ).fetchone()
        if draft is None or draft["account_status"] != "active" or float(draft["expires_at"]) <= self._now():
            raise ChannelDenied("draft is unavailable")
        namespace = draft["credential_namespace"]
        provider = self._provider_factory(self._decrypt(namespace, draft["token_cipher"]))
        message_id = provider.send_message(
            chat_id=self._decrypt(namespace, draft["chat_id_cipher"]),
            text=self._decrypt(namespace, draft["text_cipher"]),
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE channel_drafts SET status='sent', provider_message_id=? WHERE draft_id=? AND status='draft'",
                (message_id, draft_id),
            )
        return {"draft_id": draft_id, "status": "sent", "provider_message_id": message_id}

    def audit_summary(self, *, person_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT provider_account_id, update_id, disposition, recorded_at FROM channel_events WHERE person_id=? ORDER BY recorded_at DESC",
                (person_id,),
            ).fetchall()
        return [dict(row) for row in rows]
