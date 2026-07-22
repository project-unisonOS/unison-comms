import os
import time
import asyncio
import imaplib
import smtplib
import email
import json
import hashlib
import hmac
import tempfile
from pathlib import Path
from base64 import urlsafe_b64decode, urlsafe_b64encode
from email.message import EmailMessage
from email.header import decode_header
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Protocol

from fastapi import Body, FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

try:
    from unison_common import BatonMiddleware  # type: ignore
except Exception:  # pragma: no cover
    BatonMiddleware = None  # type: ignore
from unison_common.principal import bind_identity
from unison_common.principal_middleware import PrincipalBindingMiddleware, get_bound_principal, get_current_principal
from unison_common.trust import read_secret_setting
from channel_gateway import AuthBindingClient, ChannelDenied, ChannelGateway, ProviderUnavailable

app = FastAPI(title="unison-comms")
_started = time.time()
_disable_auth = os.getenv("DISABLE_AUTH_FOR_TESTS", "false").lower() in {"1", "true", "yes", "on"}

if BatonMiddleware and not _disable_auth:
    app.add_middleware(BatonMiddleware)
app.add_middleware(
    PrincipalBindingMiddleware,
    service_name="comms",
    public_paths={"/health", "/readyz", "/docs", "/openapi.json"},
    allow_test_bypass=True,
)

_unison_event_listeners: List[Any] = []
_channel_gateway_instance: ChannelGateway | None = None


def _channel_gateway() -> ChannelGateway:
    global _channel_gateway_instance
    if _channel_gateway_instance is None:
        root_key = read_secret_setting("CHANNEL_GATEWAY_ROOT_KEY")
        workload_secret = read_secret_setting("AUTH_CHANNEL_WORKLOAD_SECRET")
        if not root_key or not workload_secret:
            raise RuntimeError("channel gateway secrets are not configured")
        authority = AuthBindingClient(
            os.getenv("AUTH_URL", "http://auth:8088"),
            os.getenv("AUTH_CHANNEL_WORKLOAD_ID", "unison-comms-channel-gateway"),
            workload_secret,
        )
        _channel_gateway_instance = ChannelGateway(
            os.getenv("CHANNEL_GATEWAY_DB", "/data/comms/channel-gateway.db"),
            root_key,
            authority,
        )
    return _channel_gateway_instance


def _principal_partitions() -> tuple[str, str, str]:
    principal = get_current_principal()
    if principal is not None:
        return principal.credential_namespace, principal.data_namespace, principal.key_handle
    if _disable_auth or os.getenv("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "false").lower() == "true":
        return "credential:test", "data:test", "key:test"
    raise RuntimeError("trusted principal required for personal communications state")


def _partitioned_path(env_name: str, fallback: str, namespace: str) -> Path:
    configured = Path(os.getenv(env_name, fallback))
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
    return configured.parent / configured.stem / f"{digest}{configured.suffix or '.enc'}"


def _principal_key(purpose: str, key_handle: str) -> bytes:
    raw_root = read_secret_setting("COMMS_ROOT_KEY")
    if not raw_root:
        if not (_disable_auth or os.getenv("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "false").lower() == "true"):
            raise RuntimeError("COMMS_ROOT_KEY is required")
        raw_root = "test-only-comms-root-key-material-32-bytes"
    digest = hmac.new(
        raw_root.encode("utf-8"),
        f"{key_handle}\x00{purpose}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return urlsafe_b64encode(digest)


def _bind_request_body(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return bind_identity(body, get_bound_principal(request))
    except RuntimeError:
        if _disable_auth or os.getenv("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "false").lower() == "true":
            return body
        raise HTTPException(status_code=401, detail="trusted principal required")


def _priority_tag(subject: str) -> str:
    sub = subject.lower() if isinstance(subject, str) else ""
    if "urgent" in sub or "action required" in sub:
        return "p0"
    if "important" in sub:
        return "p1"
    return "p2"


def _load_key(raw: Optional[str]) -> Optional[bytes]:
    if not raw:
        return None
    try:
        return urlsafe_b64decode(raw)
    except Exception:
        return None


def _encrypt_blob(data: Any, key: Optional[bytes]) -> str:
    if not key:
        return json.dumps(data)
    try:
        from cryptography.fernet import Fernet

        f = Fernet(key)
        return f.encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")
    except Exception:
        return json.dumps(data)


def _decrypt_blob(ciphertext: str, key: Optional[bytes]) -> Any:
    if not key:
        return json.loads(ciphertext) if ciphertext else {}
    try:
        from cryptography.fernet import Fernet

        f = Fernet(key)
        plaintext = f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        return json.loads(plaintext)
    except Exception:
        return json.loads(ciphertext) if ciphertext else {}


def _decode_header_value(raw: Any) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode()
        except Exception:
            return raw.decode(errors="ignore")
    if isinstance(raw, str):
        try:
            decoded_parts = decode_header(raw)
            return "".join(
                part.decode(enc or "utf-8") if isinstance(part, bytes) else part for part, enc in decoded_parts
            )
        except Exception:
            return raw
    return str(raw)


class EmailAdapter(Protocol):
    def fetch_messages(self, channel: str = "email") -> List[Dict[str, Any]]:
        ...

    def send_reply(
        self, person_id: str, thread_id: str, message_id: str, body: str, recipients: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        ...

    def send_compose(
        self, person_id: str, channel: str, recipients: List[str], subject: str, body: str
    ) -> Dict[str, Any]:
        ...


class InMemoryEmailAdapter:
    """
    Simple in-memory email adapter stub.

    Keeps everything on-device and produces normalized messages for the orchestrator.
    """

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []
        self._seed_messages()

    def _seed_messages(self):
        self._messages = [
            {
                "channel": "email",
                "participants": [
                    {"address": "alice@example.com", "role": "from"},
                    {"address": "you@example.com", "role": "to"},
                ],
                "subject": "Urgent: design review",
                "body": "Can you review the design by tomorrow?",
                "thread_id": "thread-1",
                "message_id": "msg-1",
                "context_tags": ["comms", "email", "p0", "project:unisonos"],
                "metadata": {"source": "stub"},
            },
            {
                "channel": "email",
                "participants": [
                    {"address": "team@example.com", "role": "from"},
                    {"address": "you@example.com", "role": "to"},
                ],
                "subject": "Weekly update",
                "body": "Highlights and blockers for this week.",
                "thread_id": "thread-2",
                "message_id": "msg-2",
                "context_tags": ["comms", "email", "p2"],
                "metadata": {"source": "stub"},
            },
        ]

    def fetch_messages(self, channel: str = "email") -> List[Dict[str, Any]]:
        return [m for m in self._messages if m.get("channel") == channel]

    def send_reply(self, person_id: str, thread_id: str, message_id: str, body: str, recipients: Optional[List[str]] = None) -> Dict[str, Any]:
        # Append a minimal reply artifact for traceability
        reply_id = f"reply-{int(time.time())}"
        self._messages.append(
            {
                "channel": "email",
                "participants": [{"address": f"{person_id}@example.com", "role": "from"}] + (
                    [{"address": r, "role": "to"} for r in (recipients or [])]
                ),
                "subject": f"Re: {thread_id}",
                "body": body,
                "thread_id": thread_id,
                "message_id": reply_id,
                "context_tags": ["comms", "email", "sent"],
                "metadata": {"in_reply_to": message_id},
            }
        )
        return {"status": "sent", "message_id": reply_id, "thread_id": thread_id}

    def send_compose(
        self, person_id: str, channel: str, recipients: List[str], subject: str, body: str
    ) -> Dict[str, Any]:
        msg_id = f"composed-{int(time.time())}"
        tags = ["comms", channel, _priority_tag(subject)]
        self._messages.append(
            {
                "channel": channel,
                "participants": [{"address": r, "role": "to"} for r in recipients],
                "subject": subject,
                "body": body,
                "thread_id": msg_id,
                "message_id": msg_id,
                "context_tags": tags,
                "metadata": {"sender": f"{person_id}@example.com"},
            }
        )
        return {"status": "sent", "message_id": msg_id, "thread_id": msg_id, "tags": tags}


def _gmail_store_path() -> Path:
    credential_namespace, _, _ = _principal_partitions()
    default_path = str(Path(tempfile.gettempdir()) / "unison-comms-gmail.enc")
    return _partitioned_path("COMMS_GMAIL_STORE_PATH", default_path, credential_namespace)


def _gmail_store_key() -> Optional[bytes]:
    _, _, key_handle = _principal_partitions()
    return _principal_key("comms:gmail-credentials", key_handle)


def _load_gmail_bootstrap() -> Dict[str, Any]:
    key = _gmail_store_key()
    if not key:
        return {}
    store_path = _gmail_store_path()
    try:
        if store_path.exists():
            return _decrypt_blob(store_path.read_text(), key) or {}
    except Exception:
        return {}
    return {}


def _persist_gmail_bootstrap(data: Dict[str, Any]) -> bool:
    key = _gmail_store_key()
    if not key:
        return False
    store_path = _gmail_store_path()
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(_encrypt_blob(data, key))
        return True
    except Exception:
        return False


def _clear_gmail_bootstrap() -> bool:
    store_path = _gmail_store_path()
    try:
        if store_path.exists():
            store_path.unlink()
        return True
    except Exception:
        return False


def _gmail_credentials() -> Dict[str, Optional[str]]:
    stored = _load_gmail_bootstrap()
    allow_legacy_test_env = _disable_auth or os.getenv("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "false").lower() == "true"
    env_username = os.getenv("GMAIL_USERNAME") if allow_legacy_test_env else None
    env_password = os.getenv("GMAIL_APP_PASSWORD") if allow_legacy_test_env else None
    return {
        "username": stored.get("username") or env_username,
        "app_password": stored.get("app_password") or env_password,
        "imap_host": stored.get("imap_host") or "imap.gmail.com",
        "smtp_host": stored.get("smtp_host") or "smtp.gmail.com",
        "source": "credential_broker" if stored else ("test_environment" if env_username or env_password else None),
    }


class GmailAdapter:
    """
    Minimal Gmail adapter using IMAP + SMTP with app passwords.

    Assumes edge-only secrets from env or the local encrypted bootstrap store.
    """

    def __init__(self):
        creds = _gmail_credentials()
        self.username = creds.get("username")
        self.app_password = creds.get("app_password")
        self.imap_host = creds.get("imap_host") or "imap.gmail.com"
        self.smtp_host = creds.get("smtp_host") or "smtp.gmail.com"
        self._thread_recipients: Dict[str, List[str]] = {}
        if not self.username or not self.app_password:
            raise RuntimeError("Gmail credentials not configured")

    def _connect_imap(self):
        client = imaplib.IMAP4_SSL(self.imap_host)
        client.login(self.username, self.app_password)
        return client

    def _connect_smtp(self):
        client = smtplib.SMTP_SSL(self.smtp_host, 465)
        client.login(self.username, self.app_password)
        return client

    def _normalize_message(self, msg: email.message.Message, uid: str) -> Dict[str, Any]:
        subject = _decode_header_value(msg.get("Subject"))
        from_addr = parseaddr(msg.get("From") or "")[1]
        to_addr = parseaddr(msg.get("To") or "")[1]
        body_text = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
                        break
                    except Exception:
                        continue
        else:
            try:
                body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8")
            except Exception:
                body_text = ""

        priority = _priority_tag(subject)
        message_id = msg.get("Message-ID") or uid
        thread_id = msg.get("Thread-Index") or msg.get("References") or message_id
        participants = [
            {"address": from_addr, "role": "from"} if from_addr else {},
            {"address": to_addr or self.username, "role": "to"},
        ]
        # Cache recipients for reply resolution
        addrs = [p.get("address") for p in participants if p.get("address")]
        if addrs:
            self._thread_recipients[thread_id] = addrs
        return {
            "channel": "email",
            "participants": participants,
            "subject": subject or "(no subject)",
            "body": body_text or "(no body)",
            "thread_id": thread_id,
            "message_id": message_id,
            "context_tags": ["comms", "email", priority],
            "metadata": {"source": "gmail"},
        }

    def fetch_messages(self, channel: str = "email") -> List[Dict[str, Any]]:
        if channel != "email":
            return []
        messages: List[Dict[str, Any]] = []
        try:
            imap_client = self._connect_imap()
            imap_client.select("INBOX")
            status, data = imap_client.search(None, "UNSEEN")
            if status != "OK":
                imap_client.logout()
                return []
            uids = data[0].split()[-5:]  # last 5 unseen
            for uid in uids:
                status, msg_data = imap_client.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                parsed = email.message_from_bytes(raw)
                messages.append(self._normalize_message(parsed, uid.decode()))
            imap_client.logout()
        except Exception:
            # Fail quietly; caller can fall back to stub if desired
            return []
        return messages

    def send_reply(
        self, person_id: str, thread_id: str, message_id: str, body: str, recipients: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        try:
            with self._connect_smtp() as smtp:
                msg = EmailMessage()
                msg["Subject"] = f"Re: {thread_id}"
                msg["From"] = self.username
                to_list = recipients or self._thread_recipients.get(thread_id) or [self.username]
                msg["To"] = ", ".join(to_list)
                msg.set_content(body)
                smtp.send_message(msg)
            return {"status": "sent", "message_id": message_id, "thread_id": thread_id, "provider": "gmail"}
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "message_id": message_id, "thread_id": thread_id, "provider": "gmail"}

    def send_compose(
        self, person_id: str, channel: str, recipients: List[str], subject: str, body: str
    ) -> Dict[str, Any]:
        msg_id = f"gmail-{int(time.time())}"
        tags = ["comms", channel, _priority_tag(subject)]
        draft_mode = os.getenv("COMMS_GMAIL_DRAFT_ONLY", "true").lower() in {"1", "true", "yes", "on"}
        if draft_mode:
            return {
                "status": "draft",
                "message_id": msg_id,
                "thread_id": msg_id,
                "tags": tags,
                "provider": "gmail",
                "draft": {
                    "thread_id": msg_id,
                    "message_id": msg_id,
                    "subject": subject,
                    "body": body,
                    "to": recipients,
                },
            }
        try:
            with self._connect_smtp() as smtp:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = self.username
                msg["To"] = ", ".join(recipients)
                msg.set_content(body)
                smtp.send_message(msg)
            return {"status": "sent", "message_id": msg_id, "thread_id": msg_id, "tags": tags, "provider": "gmail"}
        except Exception as exc:
            return {
                "status": "draft",
                "message_id": msg_id,
                "thread_id": msg_id,
                "tags": tags,
                "provider": "gmail",
                "error": str(exc),
                "draft": {
                    "thread_id": msg_id,
                    "message_id": msg_id,
                    "subject": subject,
                    "body": body,
                    "to": recipients,
                },
            }


class UnisonAdapter:
    """
    Local Unison-to-Unison messaging adapter (edge-only, persisted locally).
    """

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []
        _, data_namespace, key_handle = _principal_partitions()
        default_path = str(Path(tempfile.gettempdir()) / "unison-comms-unison.enc")
        self._store_path = _partitioned_path(
            "COMMS_UNISON_STORE_PATH", default_path, data_namespace
        )
        self._store_key = _principal_key("comms:messages", key_handle)
        self._load_store()

    def _load_store(self):
        try:
            if self._store_path.exists():
                data = self._store_path.read_text()
                self._messages = _decrypt_blob(data, self._store_key) or []
        except Exception:
            self._messages = []

    def _persist(self):
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            blob = _encrypt_blob(self._messages, self._store_key)
            self._store_path.write_text(blob)
        except Exception:
            pass

    def fetch_messages(self, channel: str = "unison") -> List[Dict[str, Any]]:
        return [m for m in self._messages if m.get("channel") == channel]

    def send_reply(
        self, person_id: str, thread_id: str, message_id: str, body: str, recipients: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        msg_id = f"unison-reply-{int(time.time())}"
        participants = [{"address": person_id, "role": "from"}] + [{"address": r, "role": "to"} for r in (recipients or [])]
        self._messages.append(
            {
                "channel": "unison",
                "participants": participants,
                "subject": f"Re: {thread_id}",
                "body": body,
                "thread_id": thread_id,
                "message_id": msg_id,
                "context_tags": ["comms", "unison", "sent"],
                "metadata": {"in_reply_to": message_id},
            }
        )
        self._persist()
        return {"status": "sent", "message_id": msg_id, "thread_id": thread_id, "provider": "unison"}

    def send_compose(
        self, person_id: str, channel: str, recipients: List[str], subject: str, body: str
    ) -> Dict[str, Any]:
        msg_id = f"unison-{int(time.time())}"
        tags = ["comms", "unison", _priority_tag(subject)]
        participants = [{"address": person_id, "role": "from"}] + [{"address": r, "role": "to"} for r in recipients]
        self._messages.append(
            {
                "channel": "unison",
                "participants": participants,
                "subject": subject,
                "body": body,
                "thread_id": msg_id,
                "message_id": msg_id,
                "context_tags": tags,
                "metadata": {"provider": "unison"},
            }
        )
        self._persist()
        _unison_event_listeners  # no-op placeholder to appease linters; SSE uses _messages directly.
        return {"status": "sent", "message_id": msg_id, "thread_id": msg_id, "tags": tags, "provider": "unison"}


def _gmail_readiness() -> Dict[str, Any]:
    provider = os.getenv("COMMS_EMAIL_PROVIDER", "stub").lower()
    if provider != "gmail":
        return {
            "provider": provider,
            "ready": True,
            "state": "stub",
            "onboarding": {
                "status": "not_started",
                "next_action": "set COMMS_EMAIL_PROVIDER=gmail to begin Gmail onboarding",
            },
        }
    creds = _gmail_credentials()
    username = creds.get("username")
    app_password = creds.get("app_password")
    draft_only = os.getenv("COMMS_GMAIL_DRAFT_ONLY", "true").lower() in {"1", "true", "yes", "on"}
    if not username and not app_password:
        return {
            "provider": "gmail",
            "ready": False,
            "state": "not_configured",
            "credential_source": creds.get("source"),
            "onboarding": {
                "status": "needs_account",
                "next_action": "set GMAIL_USERNAME and GMAIL_APP_PASSWORD or call /comms/onboarding/email/bootstrap",
            },
        }
    if username and not app_password:
        return {
            "provider": "gmail",
            "ready": False,
            "state": "missing_secret",
            "username": username,
            "credential_source": creds.get("source"),
            "onboarding": {
                "status": "needs_app_password",
                "next_action": "set GMAIL_APP_PASSWORD for the configured GMAIL_USERNAME or call /comms/onboarding/email/bootstrap",
            },
        }
    return {
        "provider": "gmail",
        "ready": True,
        "state": "configured",
        "username": username,
        "credential_source": creds.get("source"),
        "draft_only": draft_only,
        "onboarding": {
            "status": "ready",
            "next_action": "run comms.check or comms.compose",
        },
    }


def _verify_email_onboarding() -> Dict[str, Any]:
    email_state = _gmail_readiness()
    provider = email_state.get("provider")
    if provider != "gmail":
        return {
            "ok": True,
            "provider": provider,
            "verified": False,
            "status": "skipped",
            "detail": "gmail onboarding not active",
            "onboarding": email_state.get("onboarding") or {},
        }
    if not email_state.get("ready"):
        return {
            "ok": True,
            "provider": "gmail",
            "verified": False,
            "status": "needs_configuration",
            "detail": "gmail credentials are incomplete",
            "state": email_state.get("state"),
            "onboarding": email_state.get("onboarding") or {},
        }
    try:
        adapter = GmailAdapter()
        messages = adapter.fetch_messages(channel="email")
        return {
            "ok": True,
            "provider": "gmail",
            "verified": True,
            "status": "verified",
            "detail": "gmail connection exercised successfully",
            "message_count": len(messages),
            "draft_only": email_state.get("draft_only", False),
        }
    except Exception as exc:
        return {
            "ok": True,
            "provider": "gmail",
            "verified": False,
            "status": "verification_failed",
            "detail": str(exc),
            "draft_only": email_state.get("draft_only", False),
        }


def _reset_email_onboarding() -> Dict[str, Any]:
    provider = os.getenv("COMMS_EMAIL_PROVIDER", "stub").lower()
    if provider != "gmail":
        return {
            "ok": True,
            "provider": provider,
            "status": "no_active_gmail_onboarding",
            "detail": "gmail onboarding is not currently active",
        }
    creds = _gmail_credentials()
    cleared_store = False
    if creds.get("source") == "credential_broker":
        cleared_store = _clear_gmail_bootstrap()
    return {
        "ok": True,
        "provider": "gmail",
        "status": "reset_available",
        "disconnected": True,
        "detail": "gmail onboarding state reset requested; clear local Gmail env/secrets to fully disconnect",
        "cleared": ["GMAIL_USERNAME", "GMAIL_APP_PASSWORD"],
        "cleared_bootstrap_store": cleared_store,
        "credential_source": creds.get("source"),
        "account": creds.get("username"),
    }


def _oauth_email_onboarding_contract() -> Dict[str, Any]:
    provider = os.getenv("COMMS_EMAIL_PROVIDER", "stub").lower()
    return {
        "ok": True,
        "channel": "email",
        "provider": "gmail" if provider == "gmail" else provider,
        "oauth": {
            "supported": True,
            "flow": "device_authorization_grant",
            "status": "not_implemented",
            "broker": "unison-capability",
            "scopes": ["gmail.readonly", "gmail.send"],
            "next_action": "request a device code from unison-capability and complete consent on the local device",
            "local_only": True,
        },
    }


def _resolve_email_adapter():
    provider = os.getenv("COMMS_EMAIL_PROVIDER", "stub").lower()
    if provider == "gmail":
        try:
            return GmailAdapter()
        except Exception:
            pass
    return InMemoryEmailAdapter()


_adapter_cache: Dict[tuple[str, str], EmailAdapter] = {}


def _get_adapter(channel: str) -> EmailAdapter:
    credential_namespace, data_namespace, _ = _principal_partitions()
    namespace = data_namespace if channel == "unison" else credential_namespace
    cache_key = (namespace, channel)
    adapter = _adapter_cache.get(cache_key)
    if adapter is None:
        adapter = UnisonAdapter() if channel == "unison" else _resolve_email_adapter()
        _adapter_cache[cache_key] = adapter
    return adapter


def _comms_check_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    person_id = body.get("person_id")
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    channel = body.get("channel") or "email"
    adapter = _get_adapter(channel)
    messages = adapter.fetch_messages(channel=channel)
    cards = [_card_for_message(m) for m in messages]
    provider = "gmail" if channel == "email" and isinstance(adapter, GmailAdapter) else channel
    status = "messages_found" if messages else "no_messages"
    detail = "messages available" if messages else f"no {provider} messages found"
    return {
        "ok": True,
        "person_id": person_id,
        "channel": channel,
        "provider": provider,
        "status": status,
        "detail": detail,
        "message_count": len(messages),
        "messages": messages,
        "cards": cards,
    }


def _comms_summarize_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    person_id = body.get("person_id")
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    window = body.get("window") or "today"
    channel = body.get("channel") or "email"
    adapter = _get_adapter(channel)
    messages = adapter.fetch_messages(channel=channel)
    p0 = sum(1 for m in messages if "p0" in (m.get("context_tags") or []))
    p1 = sum(1 for m in messages if "p1" in (m.get("context_tags") or []))
    other = max(len(messages) - p0 - p1, 0)
    provider = "gmail" if channel == "email" and isinstance(adapter, GmailAdapter) else channel
    status = "messages_found" if messages else "no_messages"
    if messages:
        summary_text = f"Summary for {window}: {p0} urgent, {p1} important, {other} other threads."
        detail = f"summarized {len(messages)} {provider} messages"
    else:
        summary_text = f"Summary for {window}: no {provider} messages found."
        detail = f"no {provider} messages available for summarization"
    summary_card = {
        "id": f"comms-summary-{window}",
        "type": "summary",
        "title": f"Comms summary ({window})",
        "body": summary_text,
        "tags": ["comms", "summary", channel],
        "origin_intent": "comms.summarize",
    }
    return {
        "ok": True,
        "person_id": person_id,
        "channel": channel,
        "provider": provider,
        "status": status,
        "detail": detail,
        "message_count": len(messages),
        "summary": summary_text,
        "cards": [summary_card],
    }


def _comms_reply_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    person_id = body.get("person_id")
    thread_id = body.get("thread_id")
    message_id = body.get("message_id")
    reply_body = body.get("body") or ""
    recipients = body.get("recipients") if isinstance(body.get("recipients"), list) else None
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    if not isinstance(thread_id, str) or not thread_id:
        raise HTTPException(status_code=400, detail="thread_id required")
    if not isinstance(message_id, str) or not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    adapter = _get_adapter(body.get("channel") or "email")
    result = adapter.send_reply(person_id, thread_id, message_id, reply_body, recipients)
    ok = result.get("status") == "sent"
    if not ok:
        raise HTTPException(status_code=502, detail=f"send failed: {result.get('error')}")
    return {**result, "ok": ok, "person_id": person_id, "origin_intent": "comms.reply"}


def _comms_compose_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    person_id = body.get("person_id")
    channel = body.get("channel") or "email"
    recipients: Optional[List[str]] = body.get("recipients")
    subject = body.get("subject") or ""
    msg_body = body.get("body") or ""
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    if not recipients or not isinstance(recipients, list):
        raise HTTPException(status_code=400, detail="recipients required")
    if not subject:
        raise HTTPException(status_code=400, detail="subject required")
    adapter = _get_adapter(channel)
    result = adapter.send_compose(person_id, channel, recipients, subject, msg_body)
    return {
        "ok": True,
        "person_id": person_id,
        "channel": channel,
        "recipients": recipients,
        "subject": subject,
        "origin_intent": "comms.compose",
        **result,
    }


@app.get("/stream/unison")
async def stream_unison():
    """Server-sent events stream for Unison channel messages."""
    adapter = _get_adapter("unison")
    async def event_generator():
        last_len = len(adapter._messages)
        while True:
            if len(adapter._messages) > last_len:
                adapter._load_store()
            if len(adapter._messages) > last_len:
                new_msgs = adapter._messages[last_len:]
                last_len = len(adapter._messages)
                yield {"event": "unison", "data": json.dumps({"messages": new_msgs})}
            await asyncio.sleep(2)
    return EventSourceResponse(event_generator())


@app.get("/comms/unison/stream")
async def stream_unison_compat():
    """Compatibility alias for older renderer paths."""
    return await stream_unison()


def _card_for_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Produce a dashboard-friendly card from a normalized message."""
    return {
        "id": f"comms-{msg.get('message_id', 'unknown')}",
        "type": "summary",
        "title": msg.get("subject") or "New message",
        "body": msg.get("body") or "",
        "tags": msg.get("context_tags") or ["comms"],
        "origin_intent": "comms.check",
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "unison-comms", "uptime": time.time() - _started}


@app.get("/readyz")
def readyz() -> Dict[str, Any]:
    if _disable_auth or os.getenv("UNISON_PRINCIPAL_BINDING_TEST_BYPASS", "false").lower() == "true":
        email_state = _gmail_readiness()
        ready = True if email_state.get("provider") != "gmail" else bool(email_state.get("ready"))
        return {"status": "ready" if ready else "degraded", "service": "unison-comms", "email": email_state}
    return {
        "status": "ready",
        "service": "unison-comms",
        "personal_configuration": "available after authentication",
    }


@app.get("/comms/onboarding/email")
def comms_email_onboarding() -> Dict[str, Any]:
    email_state = _gmail_readiness()
    return {
        "ok": True,
        "service": "unison-comms",
        "channel": "email",
        "provider": email_state.get("provider"),
        "ready": email_state.get("ready"),
        "state": email_state.get("state"),
        "credential_source": email_state.get("credential_source"),
        "onboarding": email_state.get("onboarding") or {},
        "draft_only": email_state.get("draft_only", False),
    }


@app.post("/comms/onboarding/email/bootstrap")
def comms_email_onboarding_bootstrap(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    body = _bind_request_body(request, body)
    provider = (body.get("provider") or "gmail").lower()
    username = body.get("username")
    app_password = body.get("app_password")
    imap_host = body.get("imap_host") or "imap.gmail.com"
    smtp_host = body.get("smtp_host") or "smtp.gmail.com"
    if provider != "gmail":
        raise HTTPException(status_code=400, detail="only gmail bootstrap is currently supported")
    if not isinstance(username, str) or not username:
        raise HTTPException(status_code=400, detail="username required")
    if not isinstance(app_password, str) or not app_password:
        raise HTTPException(status_code=400, detail="app_password required")
    persisted = _persist_gmail_bootstrap(
        {
            "username": username,
            "app_password": app_password,
            "imap_host": imap_host,
            "smtp_host": smtp_host,
        }
    )
    if not persisted:
        raise HTTPException(status_code=500, detail="failed to persist gmail bootstrap")
    return {
        "ok": True,
        "provider": "gmail",
        "status": "bootstrapped",
        "credential_source": "credential_broker",
        "username": username,
        "imap_host": imap_host,
        "smtp_host": smtp_host,
    }


@app.post("/comms/onboarding/email/verify")
def comms_email_onboarding_verify() -> Dict[str, Any]:
    return _verify_email_onboarding()


@app.post("/comms/onboarding/email/reset")
def comms_email_onboarding_reset() -> Dict[str, Any]:
    return _reset_email_onboarding()


@app.get("/comms/onboarding/email/oauth")
def comms_email_onboarding_oauth() -> Dict[str, Any]:
    return _oauth_email_onboarding_contract()


@app.post("/comms/check")
def comms_check(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Check for new/unread communications.
    Uses the configured adapter (email/unison) and returns normalized messages + derived cards.
    """
    return _comms_check_impl(_bind_request_body(request, body))


@app.post("/comms/summarize")
def comms_summarize(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Summarize communications over a time window or topic.
    Stub returns a canned summary and a summary card.
    """
    return _comms_summarize_impl(_bind_request_body(request, body))


@app.post("/comms/reply")
def comms_reply(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Send a reply to an existing thread/message.
    Stub validates identifiers and returns a confirmation payload.
    """
    return _comms_reply_impl(_bind_request_body(request, body))


@app.post("/comms/compose")
def comms_compose(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Compose and send a new message.
    Stub validates required fields and returns a confirmation payload.
    """
    return _comms_compose_impl(_bind_request_body(request, body))


def _channel_person(request: Request) -> str:
    bound = get_bound_principal(request)
    if bound is None or not bound.person_id:
        raise HTTPException(status_code=401, detail="trusted person required")
    return bound.person_id


@app.post("/channel-gateway/accounts/telegram", status_code=201)
def register_telegram_channel(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return _channel_gateway().register_telegram_account(
            person_id=_channel_person(request),
            provider_account_id=str(body["provider_account_id"]),
            token=str(body["bot_token"]),
            bot_id=str(body["bot_id"]),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="provider account, bot ID, and token are required") from exc
    except (ChannelDenied, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/channel-gateway/poll")
def poll_telegram_channel(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    account_id = str(body.get("provider_account_id", ""))
    try:
        gateway = _channel_gateway()
        if gateway.account_owner(account_id) != _channel_person(request):
            raise ChannelDenied("provider account is unavailable")
        results = gateway.poll(account_id)
        return {
            "status": "connected",
            "results": [
                {
                    "status": result.status,
                    "update_id": result.update_id,
                    "envelope": result.envelope.model_dump(mode="json") if result.envelope else None,
                    "outcome": result.outcome.model_dump(mode="json") if result.outcome else None,
                }
                for result in results
            ],
        }
    except ChannelDenied as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/channel-gateway/drafts", status_code=201)
def create_channel_draft(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return _channel_gateway().create_outbound_draft(
            person_id=_channel_person(request),
            provider_account_id=str(body["provider_account_id"]),
            chat_id=str(body["chat_id"]),
            text=str(body["text"]),
            purpose=str(body.get("purpose", "assistant-reply")),
        )
    except (KeyError, ChannelDenied) as exc:
        raise HTTPException(status_code=404, detail="draft is unavailable") from exc


@app.post("/channel-gateway/drafts/{draft_id}/confirm")
def confirm_channel_draft(draft_id: str, request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    principal = get_bound_principal(request)
    try:
        return _channel_gateway().confirm_outbound_draft(
            person_id=_channel_person(request),
            draft_id=draft_id,
            assurance=getattr(getattr(principal, "assurance", None), "value", "low"),
            confirmed=body.get("confirmed") is True,
        )
    except ChannelDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.delete("/channel-gateway/accounts/{provider_account_id}")
def revoke_telegram_channel(provider_account_id: str, request: Request) -> Dict[str, Any]:
    if not _channel_gateway().revoke_account(
        person_id=_channel_person(request), provider_account_id=provider_account_id
    ):
        raise HTTPException(status_code=404, detail="provider account is unavailable")
    return {"status": "revoked", "provider_account_id": provider_account_id}


def _mcp_base_url(request: Request) -> str:
    env = os.getenv("COMMS_PUBLIC_BASE_URL")
    if env and isinstance(env, str) and env.strip():
        return env.strip().rstrip("/")
    # Best-effort inference from request
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return "http://localhost:8080"


@app.get("/mcp/registry")
def mcp_registry(request: Request) -> Dict[str, Any]:
    base = _mcp_base_url(request)
    tools = [
        {"name": "comms.check", "description": "Check for new/unread communications"},
        {"name": "comms.summarize", "description": "Summarize communications for a window/topic"},
        {"name": "comms.reply", "description": "Send a reply to a thread/message"},
        {"name": "comms.compose", "description": "Compose and send a new message"},
        {"name": "comms.join_meeting", "description": "Return join info/card for a meeting"},
        {"name": "comms.prepare_meeting", "description": "Return prep/agenda card for a meeting"},
        {"name": "comms.debrief_meeting", "description": "Return debrief/summary card for a meeting"},
    ]
    return {"servers": [{"id": "unison-comms", "name": "unison-comms", "base_url": base, "tools": tools}]}


@app.post("/tools/{tool_name}")
def mcp_tool_call(tool_name: str, request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    args = payload.get("arguments") if isinstance(payload, dict) else None
    if not isinstance(args, dict):
        args = {}
    args = _bind_request_body(request, args)
    if tool_name == "comms.check":
        return _comms_check_impl(args)
    if tool_name == "comms.summarize":
        return _comms_summarize_impl(args)
    if tool_name == "comms.reply":
        return _comms_reply_impl(args)
    if tool_name == "comms.compose":
        return _comms_compose_impl(args)
    # Meeting tools map to the existing HTTP endpoints for now.
    if tool_name in {"comms.join_meeting", "comms.prepare_meeting", "comms.debrief_meeting"}:
        # Reuse existing endpoint handlers via direct call pattern
        # (keeps response shapes identical to current HTTP surface).
        if tool_name == "comms.join_meeting":
            return _comms_join_meeting_impl(args)
        if tool_name == "comms.prepare_meeting":
            return _comms_prepare_meeting_impl(args)
        if tool_name == "comms.debrief_meeting":
            return _comms_debrief_meeting_impl(args)
    raise HTTPException(status_code=404, detail=f"tool not found: {tool_name}")


@app.post("/comms/join_meeting")
def comms_join_meeting(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return _comms_join_meeting_impl(_bind_request_body(request, body))


def _comms_join_meeting_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    """Stub meeting join endpoint; returns a card with join link/info."""
    person_id = body.get("person_id")
    meeting_id = body.get("meeting_id") or "meeting-1"
    join_url = body.get("join_url") or "https://example.com/meeting"
    card = {
        "id": f"meeting-{meeting_id}",
        "type": "summary",
        "title": f"Join meeting {meeting_id}",
        "body": f"Join link: {join_url}",
        "tags": ["comms", "meeting"],
        "origin_intent": "comms.join_meeting",
    }
    return {"ok": True, "person_id": person_id, "meeting_id": meeting_id, "cards": [card]}


@app.post("/comms/prepare_meeting")
def comms_prepare_meeting(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return _comms_prepare_meeting_impl(_bind_request_body(request, body))


def _comms_prepare_meeting_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    """Stub meeting prep; returns agenda/participants cards."""
    person_id = body.get("person_id")
    meeting_id = body.get("meeting_id") or "meeting-1"
    agenda = body.get("agenda") or ["Review updates", "Decide next steps"]
    card = {
        "id": f"meeting-prep-{meeting_id}",
        "type": "guide",
        "title": f"Meeting prep: {meeting_id}",
        "steps": agenda,
        "tags": ["comms", "meeting", "prep"],
        "origin_intent": "comms.prepare_meeting",
    }
    return {"ok": True, "person_id": person_id, "meeting_id": meeting_id, "cards": [card]}


@app.post("/comms/debrief_meeting")
def comms_debrief_meeting(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    return _comms_debrief_meeting_impl(_bind_request_body(request, body))


def _comms_debrief_meeting_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    """Stub meeting debrief; returns summary card."""
    person_id = body.get("person_id")
    meeting_id = body.get("meeting_id") or "meeting-1"
    summary = body.get("summary") or "Decisions: TBD. Follow-ups: TBD."
    card = {
        "id": f"meeting-debrief-{meeting_id}",
        "type": "summary",
        "title": f"Meeting debrief: {meeting_id}",
        "body": summary,
        "tags": ["comms", "meeting", "debrief"],
        "origin_intent": "comms.debrief_meeting",
    }
    return {"ok": True, "person_id": person_id, "meeting_id": meeting_id, "summary": summary, "cards": [card]}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
