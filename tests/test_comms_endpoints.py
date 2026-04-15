import pathlib
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "src"))

import main  # noqa: E402
from main import app  # noqa: E402


def test_comms_check_returns_stub_card():
    client = TestClient(app)
    resp = client.post("/comms/check", json={"person_id": "p1", "channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["person_id"] == "p1"
    assert body["status"] == "messages_found"
    assert body["message_count"] >= 1
    assert isinstance(body.get("messages"), list) and body["messages"]
    msg = body["messages"][0]
    assert msg["channel"] == "email"
    assert "comms" in msg.get("context_tags", [])
    assert isinstance(body.get("cards"), list) and body["cards"][0]["origin_intent"] == "comms.check"


def test_comms_summarize_returns_summary():
    client = TestClient(app)
    resp = client.post("/comms/summarize", json={"person_id": "p1", "window": "today"})
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert body["status"] == "messages_found"
    assert body["message_count"] >= 1
    assert body["cards"][0]["origin_intent"] == "comms.summarize"


def test_comms_summarize_uses_adapter_messages(monkeypatch):
    class _SummaryAdapter:
        def fetch_messages(self, channel: str = "email"):
            return [
                {"context_tags": ["comms", "email", "p0"]},
                {"context_tags": ["comms", "email", "p1"]},
                {"context_tags": ["comms", "email", "p2"]},
            ]

        def send_reply(self, person_id, thread_id, message_id, body, recipients=None):
            return {"status": "sent"}

        def send_compose(self, person_id, channel, recipients, subject, body):
            return {"status": "sent"}

    monkeypatch.setattr(main, "_get_adapter", lambda channel: _SummaryAdapter())

    client = TestClient(app)
    resp = client.post("/comms/summarize", json={"person_id": "p1", "window": "today", "channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "messages_found"
    assert body["message_count"] == 3
    assert body["summary"] == "Summary for today: 1 urgent, 1 important, 1 other threads."
    assert body["cards"][0]["origin_intent"] == "comms.summarize"


def test_comms_check_reports_no_messages(monkeypatch):
    class _EmptyAdapter:
        def fetch_messages(self, channel: str = "email"):
            return []

        def send_reply(self, person_id, thread_id, message_id, body, recipients=None):
            return {"status": "sent"}

        def send_compose(self, person_id, channel, recipients, subject, body):
            return {"status": "sent"}

    monkeypatch.setattr(main, "_get_adapter", lambda channel: _EmptyAdapter())

    client = TestClient(app)
    resp = client.post("/comms/check", json={"person_id": "p1", "channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_messages"
    assert body["message_count"] == 0
    assert body["detail"] == "no email messages found"


def test_comms_summarize_reports_no_messages(monkeypatch):
    class _EmptyAdapter:
        def fetch_messages(self, channel: str = "email"):
            return []

        def send_reply(self, person_id, thread_id, message_id, body, recipients=None):
            return {"status": "sent"}

        def send_compose(self, person_id, channel, recipients, subject, body):
            return {"status": "sent"}

    monkeypatch.setattr(main, "_get_adapter", lambda channel: _EmptyAdapter())

    client = TestClient(app)
    resp = client.post("/comms/summarize", json={"person_id": "p1", "window": "today", "channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_messages"
    assert body["message_count"] == 0
    assert body["summary"] == "Summary for today: no email messages found."
    assert body["detail"] == "no email messages available for summarization"


def test_comms_reply_requires_ids():
    client = TestClient(app)
    resp = client.post("/comms/reply", json={"person_id": "p1", "thread_id": "t1", "message_id": "m1", "body": "ok"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["origin_intent"] == "comms.reply"


def test_comms_compose_requires_recipients_and_subject():
    client = TestClient(app)
    resp = client.post(
        "/comms/compose",
        json={"person_id": "p1", "channel": "email", "recipients": ["a@example.com"], "subject": "Hi", "body": "Hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["origin_intent"] == "comms.compose"
    # ensure composed message stored and tagged
    resp2 = client.post("/comms/check", json={"person_id": "p1", "channel": "email"})
    messages = resp2.json()["messages"]
    assert any(m for m in messages if m.get("message_id") == body["message_id"])


def test_unison_compose_and_check():
    client = TestClient(app)
    resp = client.post(
        "/comms/compose",
        json={"person_id": "u1", "channel": "unison", "recipients": ["u2"], "subject": "Hello", "body": "Hi there"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body.get("provider") == "unison"
    resp2 = client.post("/comms/check", json={"person_id": "u2", "channel": "unison"})
    msgs = resp2.json()["messages"]
    assert any(m for m in msgs if m.get("message_id") == body["message_id"])


def test_meeting_stubs_return_cards():
    client = TestClient(app)
    join = client.post("/comms/join_meeting", json={"person_id": "p1", "meeting_id": "m1", "join_url": "https://x"})
    assert join.status_code == 200
    assert join.json()["cards"][0]["origin_intent"] == "comms.join_meeting"

    prep = client.post("/comms/prepare_meeting", json={"person_id": "p1", "meeting_id": "m1"})
    assert prep.status_code == 200
    assert prep.json()["cards"][0]["origin_intent"] == "comms.prepare_meeting"

    debrief = client.post("/comms/debrief_meeting", json={"person_id": "p1", "meeting_id": "m1"})
    assert debrief.status_code == 200
    assert debrief.json()["cards"][0]["origin_intent"] == "comms.debrief_meeting"
