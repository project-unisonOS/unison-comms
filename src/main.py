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


def _sample_message(channel: str = "email") -> Dict[str, Any]:
    """Return a stub normalized message shape for testing and UI placeholder data."""
    return {
        "channel": channel,
        "participants": [
            {"address": "sender@example.com", "role": "from"},
            {"address": "you@example.com", "role": "to"},
        ],
        "subject": "Sample message",
        "body": "This is a placeholder message body.",
        "thread_id": "sample-thread",
        "message_id": "sample-message",
        "context_tags": ["comms", channel, "p1"],
        "metadata": {"source": "stub"},
    }


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
    Stub implementation returns a static message and derived card.
    """
    person_id = body.get("person_id") or "local-user"
    if not isinstance(person_id, str) or not person_id:
        raise HTTPException(status_code=400, detail="person_id required")
    channel = body.get("channel") or "email"
    message = _sample_message(channel=channel)
    card = _card_for_message(message)
    return {"ok": True, "person_id": person_id, "messages": [message], "cards": [card]}


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
    return {
        "ok": True,
        "person_id": person_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "status": "sent",
        "origin_intent": "comms.reply",
    }


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
    return {
        "ok": True,
        "person_id": person_id,
        "channel": channel,
        "recipients": recipients,
        "subject": subject,
        "message_id": f"composed-{int(time.time())}",
        "status": "sent",
        "origin_intent": "comms.compose",
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
