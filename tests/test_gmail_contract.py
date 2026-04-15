import os
import pathlib
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "src"))

import main  # noqa: E402


class _FailingGmailAdapter:
    def fetch_messages(self, channel: str = "email"):
        return []

    def send_reply(self, person_id, thread_id, message_id, body, recipients=None):
        return {"status": "failed", "provider": "gmail", "error": "smtp unavailable"}

    def send_compose(self, person_id, channel, recipients, subject, body):
        return {
            "status": "draft",
            "provider": "gmail",
            "draft": {
                "thread_id": "gmail-draft-thread",
                "message_id": "gmail-draft-message",
                "subject": subject,
                "body": body,
                "to": recipients,
            },
            "tags": ["comms", channel, "p2"],
        }


class _HealthyGmailAdapter:
    def fetch_messages(self, channel: str = "email"):
        return [{"message_id": "m1"}, {"message_id": "m2"}]


def test_readyz_reports_gmail_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("COMMS_GMAIL_STORE_PATH", str(tmp_path / "gmail.json"))
    monkeypatch.setenv("COMMS_GMAIL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.delenv("GMAIL_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "unison-comms"
    assert body["email"]["provider"] == "gmail"
    assert body["email"]["ready"] is False
    assert body["email"]["state"] == "not_configured"
    assert body["email"]["onboarding"]["status"] == "needs_account"


def test_email_onboarding_endpoint_reports_missing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("COMMS_GMAIL_STORE_PATH", str(tmp_path / "gmail.json"))
    monkeypatch.setenv("COMMS_GMAIL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("GMAIL_USERNAME", "user@example.com")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    resp = client.get("/comms/onboarding/email")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "gmail"
    assert body["ready"] is False
    assert body["state"] == "missing_secret"
    assert body["onboarding"]["status"] == "needs_app_password"


def test_email_onboarding_bootstrap_persists_local_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("COMMS_GMAIL_STORE_PATH", str(tmp_path / "gmail.json"))
    monkeypatch.setenv("COMMS_GMAIL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.delenv("GMAIL_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    resp = client.post(
        "/comms/onboarding/email/bootstrap",
        json={
            "provider": "gmail",
            "username": "user@example.com",
            "app_password": "app-password",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "bootstrapped"
    assert body["credential_source"] == "bootstrap_store"

    ready = client.get("/comms/onboarding/email")
    ready_body = ready.json()
    assert ready_body["ready"] is True
    assert ready_body["state"] == "configured"
    assert ready_body["credential_source"] == "bootstrap_store"


def test_comms_compose_returns_gmail_draft_shape(monkeypatch):
    monkeypatch.setattr(main, "_get_adapter", lambda channel: _FailingGmailAdapter())

    client = TestClient(main.app)
    resp = client.post(
        "/comms/compose",
        json={
            "person_id": "p1",
            "channel": "email",
            "recipients": ["a@example.com"],
            "subject": "Re: Project UnisonOS",
            "body": "Please draft a reply",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["origin_intent"] == "comms.compose"
    assert body["status"] == "draft"
    assert body["provider"] == "gmail"
    assert body["draft"]["thread_id"] == "gmail-draft-thread"
    assert body["draft"]["message_id"] == "gmail-draft-message"
    assert body["draft"]["to"] == ["a@example.com"]


def test_email_onboarding_verify_reports_needs_configuration(monkeypatch):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.delenv("GMAIL_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    resp = client.post("/comms/onboarding/email/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["verified"] is False
    assert body["status"] == "needs_configuration"
    assert body["state"] == "not_configured"


def test_email_onboarding_verify_reports_verified(monkeypatch):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_USERNAME", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(main, "GmailAdapter", _HealthyGmailAdapter)

    client = TestClient(main.app)
    resp = client.post("/comms/onboarding/email/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["verified"] is True
    assert body["status"] == "verified"
    assert body["message_count"] == 2


def test_email_onboarding_reset_reports_reset_available(monkeypatch):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_USERNAME", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")

    client = TestClient(main.app)
    resp = client.post("/comms/onboarding/email/reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "gmail"
    assert body["status"] == "reset_available"
    assert body["disconnected"] is True
    assert body["credential_source"] == "env"
    assert body["cleared_bootstrap_store"] is False
    assert "GMAIL_USERNAME" in body["cleared"]
    assert "GMAIL_APP_PASSWORD" in body["cleared"]


def test_email_onboarding_reset_clears_bootstrap_store(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("COMMS_GMAIL_STORE_PATH", str(tmp_path / "gmail.json"))
    monkeypatch.setenv("COMMS_GMAIL_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.delenv("GMAIL_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    bootstrap = client.post(
        "/comms/onboarding/email/bootstrap",
        json={
            "provider": "gmail",
            "username": "user@example.com",
            "app_password": "app-password",
        },
    )
    assert bootstrap.status_code == 200

    resp = client.post("/comms/onboarding/email/reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["credential_source"] == "bootstrap_store"
    assert body["cleared_bootstrap_store"] is True

    ready = client.get("/comms/onboarding/email")
    ready_body = ready.json()
    assert ready_body["ready"] is False
    assert ready_body["state"] == "not_configured"


def test_email_onboarding_reset_reports_no_active_gmail(monkeypatch):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "stub")
    monkeypatch.delenv("GMAIL_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    client = TestClient(main.app)
    resp = client.post("/comms/onboarding/email/reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "no_active_gmail_onboarding"


def test_email_onboarding_oauth_contract(monkeypatch):
    monkeypatch.setenv("COMMS_EMAIL_PROVIDER", "gmail")

    client = TestClient(main.app)
    resp = client.get("/comms/onboarding/email/oauth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["channel"] == "email"
    assert body["provider"] == "gmail"
    assert body["oauth"]["supported"] is True
    assert body["oauth"]["flow"] == "device_authorization_grant"
    assert body["oauth"]["status"] == "not_implemented"
    assert body["oauth"]["broker"] == "unison-capability"
