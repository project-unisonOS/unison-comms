import os
import time
import imaplib
import smtplib
import email
import json
from pathlib import Path
from base64 import urlsafe_b64decode
from email.message import EmailMessage
from email.header import decode_header
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Protocol

from fastapi import Body, FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

try:
    from unison_common import BatonMiddleware  # type: ignore
except Exception:  # pragma: no cover
    BatonMiddleware = None  # type: ignore

app = FastAPI(title="unison-comms")
_started = time.time()
_disable_auth = os.getenv("DISABLE_AUTH_FOR_TESTS", "false").lower() in {"1", "true", "yes", "on"}

if BatonMiddleware and not _disable_auth:
    app.add_middleware(BatonMiddleware)

_unison_event_listeners: List[Any] = []


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


class GmailAdapter:
    """
    Minimal Gmail adapter using IMAP + SMTP with app passwords.

    Assumes edge-only secrets set via env:
    - GMAIL_USERNAME (the mailbox/user)
    - GMAIL_APP_PASSWORD (app password generated after enabling 2FA)
    """

    def __init__(self):
        self.username = os.getenv("GMAIL_USERNAME")
        self.app_password = os.getenv("GMAIL_APP_PASSWORD")
        self.imap_host = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
        self.smtp_host = os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com")
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
        try:
            with self._connect_smtp() as smtp:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = self.username
                msg["To"] = ", ".join(recipients)
                msg.set_content(body)
                smtp.send_message(msg)
        except Exception:
            pass
        tags = ["comms", channel, _priority_tag(subject)]
        return {"status": "sent", "message_id": msg_id, "thread_id": msg_id, "tags": tags, "provider": "gmail"}


class UnisonAdapter:
    """
    Local Unison-to-Unison messaging adapter (edge-only, persisted locally).
    """

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []
        self._store_path = Path(os.getenv("COMMS_UNISON_STORE_PATH", "/tmp/unison-comms-unison.json"))
        # Default to a generated key per node if none is provided, to keep store encrypted by default.
        env_key = os.getenv("COMMS_UNISON_KEY")
        if env_key:
            self._store_key = _load_key(env_key)
        else:
            try:
                from cryptography.fernet import Fernet
                gen = Fernet.generate_key()
                self._store_key = gen
            except Exception:
                self._store_key = None
        if not self._store_key:
            raise RuntimeError("Unison adapter requires COMMS_UNISON_KEY or cryptography support to encrypt the store")
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


def _resolve_email_adapter():
    provider = os.getenv("COMMS_EMAIL_PROVIDER", "stub").lower()
    if provider == "gmail":
        try:
            return GmailAdapter()
        except Exception:
            pass
    return InMemoryEmailAdapter()


_email_adapter = _resolve_email_adapter()
_unison_adapter = UnisonAdapter()


def _get_adapter(channel: str) -> EmailAdapter:
    if channel == "unison":
        return _unison_adapter
    return _email_adapter


@app.get("/stream/unison")
async def stream_unison():
    """Server-sent events stream for Unison channel messages."""
    async def event_generator():
        last_len = len(_unison_adapter._messages)
        while True:
            if len(_unison_adapter._messages) > last_len:
                new_msgs = _unison_adapter._messages[last_len:]
                last_len = len(_unison_adapter._messages)
                yield {"event": "unison", "data": json.dumps({"messages": new_msgs})}
            await asyncio.sleep(2)
    return EventSourceResponse(event_generator())


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
    return {"status": "ready", "service": "unison-comms"}


@app.post("/comms/check")
def comms_check(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Check for new/unread communications.
    Uses the configured adapter (email/unison) and returns normalized messages + derived cards.
    """
    person_id = body.get("person_id") or "local-user"
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    channel = body.get("channel") or "email"
    adapter = _get_adapter(channel)
    messages = adapter.fetch_messages(channel=channel)
    cards = [_card_for_message(m) for m in messages]
    return {"ok": True, "person_id": person_id, "messages": messages, "cards": cards}


@app.post("/comms/summarize")
def comms_summarize(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Summarize communications over a time window or topic.
    Stub returns a canned summary and a summary card.
    """
    person_id = body.get("person_id") or "local-user"
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    window = body.get("window") or "today"
    summary_text = f"Summary for {window}: 1 important thread, 2 low-priority threads."
    summary_card = {
        "id": f"comms-summary-{window}",
        "type": "summary",
        "title": f"Comms summary ({window})",
        "body": summary_text,
        "tags": ["comms", "summary"],
        "origin_intent": "comms.summarize",
    }
    return {"ok": True, "person_id": person_id, "summary": summary_text, "cards": [summary_card]}


@app.post("/comms/reply")
def comms_reply(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Send a reply to an existing thread/message.
    Stub validates identifiers and returns a confirmation payload.
    """
    person_id = body.get("person_id") or "local-user"
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


@app.post("/comms/compose")
def comms_compose(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Compose and send a new message.
    Stub validates required fields and returns a confirmation payload.
    """
    person_id = body.get("person_id") or "local-user"
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


@app.post("/comms/join_meeting")
def comms_join_meeting(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Stub meeting join endpoint; returns a card with join link/info."""
    person_id = body.get("person_id") or "local-user"
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
def comms_prepare_meeting(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Stub meeting prep; returns agenda/participants cards."""
    person_id = body.get("person_id") or "local-user"
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
def comms_debrief_meeting(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Stub meeting debrief; returns summary card."""
    person_id = body.get("person_id") or "local-user"
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

    uvicorn.run(app, host="0.0.0.0", port=8080)
