import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException

try:
    from unison_common import BatonMiddleware  # type: ignore
except Exception:  # pragma: no cover
    BatonMiddleware = None  # type: ignore

app = FastAPI(title="unison-comms")
_started = time.time()
_disable_auth = os.getenv("DISABLE_AUTH_FOR_TESTS", "false").lower() in {"1", "true", "yes", "on"}

if BatonMiddleware and not _disable_auth:
    app.add_middleware(BatonMiddleware)


def _priority_tag(subject: str) -> str:
    sub = subject.lower()
    if "urgent" in sub or "action required" in sub:
        return "p0"
    if "important" in sub:
        return "p1"
    return "p2"


class EmailAdapter:
    """
    Simple in-memory email adapter stub.

    In production this would call a provider (IMAP/SMTP/OAuth), but for now it keeps
    everything on-device and produces normalized messages for the orchestrator.
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

    def send_reply(self, person_id: str, thread_id: str, message_id: str, body: str) -> Dict[str, Any]:
        # Append a minimal reply artifact for traceability
        reply_id = f"reply-{int(time.time())}"
        self._messages.append(
            {
                "channel": "email",
                "participants": [{"address": f"{person_id}@example.com", "role": "from"}],
                "subject": f"Re: {thread_id}",
                "body": body,
                "thread_id": thread_id,
                "message_id": reply_id,
                "context_tags": ["comms", "email", "sent"],
                "metadata": {"in_reply_to": message_id},
            }
        )
        return {"status": "sent", "message_id": reply_id, "thread_id": thread_id}

    def send_compose(self, person_id: str, channel: str, recipients: List[str], subject: str, body: str) -> Dict[str, Any]:
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


_email_adapter = EmailAdapter()


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
    Uses the in-memory email adapter stub and returns normalized messages + derived cards.
    """
    person_id = body.get("person_id") or "local-user"
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    channel = body.get("channel") or "email"
    messages = _email_adapter.fetch_messages(channel=channel)
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
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    if not isinstance(thread_id, str) or not thread_id:
        raise HTTPException(status_code=400, detail="thread_id required")
    if not isinstance(message_id, str) or not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    result = _email_adapter.send_reply(person_id, thread_id, message_id, reply_body)
    return {**result, "ok": True, "person_id": person_id, "origin_intent": "comms.reply"}


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
    result = _email_adapter.send_compose(person_id, channel, recipients, subject, msg_body)
    return {
        "ok": True,
        "person_id": person_id,
        "channel": channel,
        "recipients": recipients,
        "subject": subject,
        "origin_intent": "comms.compose",
        **result,
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
