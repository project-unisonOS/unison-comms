from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

AUTH_SRC = Path(__file__).resolve().parents[2] / "unison-auth" / "src"
if AUTH_SRC.exists():
    sys.path.insert(0, str(AUTH_SRC))

from channel_gateway import ChannelDenied, ChannelGateway, FakeTelegramProvider, ProviderUnavailable
from identity_store import IdentityConflict, IdentityNotFound, IdentityStore


def telegram_update(update_id: int, sender: int, now: float, text: str, *, chat_type: str = "private"):
    return {
        "update_id": update_id,
        "message": {
            "date": int(now),
            "text": text,
            "from": {"id": sender},
            "chat": {"id": sender, "type": chat_type},
        },
    }


@pytest.fixture()
def system(tmp_path):
    clock = [1_800_000_000.0]
    identity = IdentityStore(str(tmp_path / "identity.db"))
    person = identity.bootstrap_first_person(
        confirmed=True,
        login_handle="alex",
        display_name="Alex",
        household_name="Home",
        password_hash="hash",
    )
    provider = FakeTelegramProvider()
    gateway = ChannelGateway(
        str(tmp_path / "channels.db"),
        "test-root-key-material-is-long-enough",
        identity,
        lambda _token: provider,
        now=lambda: clock[0],
        rate_limit_per_minute=4,
    )
    account_id = "bot-alex"
    gateway.register_telegram_account(
        person_id=person["person_id"], provider_account_id=account_id, token="secret-token-alex", bot_id="bot-1"
    )
    return clock, identity, person, provider, gateway, account_id, tmp_path


def pair(system, sender=101):
    clock, identity, person, provider, gateway, account_id, _ = system
    code, _challenge = identity.create_channel_pairing(
        person_id=person["person_id"],
        provider="telegram",
        provider_account_id=account_id,
        local_assurance="passkey",
    )
    provider.updates.append(telegram_update(1, sender, clock[0], f"/pair {code}"))
    assert gateway.poll(account_id)[0].status == "paired"
    provider.updates.clear()
    return sender


def test_two_people_are_independent_and_credentials_are_encrypted(system):
    clock, identity, first, provider, gateway, first_account, tmp_path = system
    token, invitation = identity.create_invitation(
        invited_by_person_id=first["person_id"], household_id=first["household_id"]
    )
    second = identity.accept_invitation(
        invitation_token=token, login_handle="sam", display_name="Sam", password_hash="hash2"
    )
    gateway.register_telegram_account(
        person_id=second["person_id"], provider_account_id="bot-sam", token="secret-token-sam", bot_id="bot-2"
    )
    with sqlite3.connect(tmp_path / "channels.db") as connection:
        stored = " ".join(row[0] for row in connection.execute("SELECT token_cipher FROM provider_accounts"))
    assert "secret-token" not in stored
    with pytest.raises(ChannelDenied):
        gateway.create_outbound_draft(
            person_id=second["person_id"], provider_account_id=first_account, chat_id="101", text="hello", purpose="reply"
        )


def test_pairing_requires_local_step_up_is_one_use_and_blocks_reassignment(system):
    clock, identity, person, provider, gateway, account_id, _ = system
    with pytest.raises(IdentityConflict):
        identity.create_channel_pairing(
            person_id=person["person_id"], provider="telegram", provider_account_id=account_id, local_assurance="low"
        )
    sender = pair(system)
    binding = identity.resolve_channel_binding(
        provider="telegram", provider_account_id=account_id, external_subject=str(sender)
    )
    assert binding and binding["person_id"] == person["person_id"]
    with pytest.raises(IdentityNotFound):
        identity.complete_channel_pairing_by_code(
            pairing_code="000000", provider="telegram", provider_account_id=account_id,
            external_subject=str(sender)
        )
    invitation_token, _ = identity.create_invitation(
        invited_by_person_id=person["person_id"], household_id=person["household_id"]
    )
    second = identity.accept_invitation(
        invitation_token=invitation_token, login_handle="sam2", display_name="Sam", password_hash="hash2"
    )
    second_code, _ = identity.create_channel_pairing(
        person_id=second["person_id"], provider="telegram", provider_account_id=account_id,
        local_assurance="high"
    )
    with pytest.raises(IdentityConflict):
        identity.complete_channel_pairing_by_code(
            pairing_code=second_code, provider="telegram", provider_account_id=account_id,
            external_subject=str(sender)
        )


def test_private_safe_message_normalizes_and_stolen_subject_is_denied(system):
    clock, identity, person, provider, gateway, account_id, _ = system
    pair(system)
    provider.updates.extend([
        telegram_update(2, 999, clock[0], "stolen token attempt"),
        telegram_update(3, 101, clock[0], "summarize my day"),
    ])
    results = gateway.poll(account_id)
    assert [result.status for result in results] == ["unbound-denied", "accepted"]
    assert results[1].envelope.bound_person_id == person["person_id"]
    assert results[1].envelope.assurance.value == "low"
    assert results[1].outcome.privacy_notice


def test_low_assurance_sensitive_and_recovery_requests_always_step_up(system):
    clock, _identity, _person, provider, gateway, account_id, _ = system
    pair(system)
    provider.updates.extend([
        telegram_update(2, 101, clock[0], "wire a payment"),
        telegram_update(3, 101, clock[0], "reset password with recovery code"),
    ])
    results = gateway.poll(account_id)
    assert all(result.status == "step-up-required" for result in results)
    assert all(result.envelope.step_up_required for result in results)


def test_replay_duplicate_delay_non_private_and_rate_limits_fail_closed(system):
    clock, _identity, _person, provider, gateway, account_id, _ = system
    pair(system)
    provider.updates.extend([
        telegram_update(2, 101, clock[0] - 301, "too old"),
        telegram_update(3, 101, clock[0], "group", chat_type="group"),
        telegram_update(4, 101, clock[0], "one"),
        telegram_update(4, 101, clock[0], "duplicate"),
        telegram_update(5, 101, clock[0], "over limit"),
    ])
    statuses = [item.status for item in gateway.poll(account_id)]
    assert statuses == ["delayed-rejected", "unsupported-or-non-private", "accepted", "replay-rejected", "rate-limited"]


def test_outage_preserves_cursor_then_reconnects(system):
    clock, _identity, _person, provider, gateway, account_id, tmp_path = system
    pair(system)
    provider.updates.append(telegram_update(2, 101, clock[0], "after outage"))
    provider.available = False
    with pytest.raises(ProviderUnavailable):
        gateway.poll(account_id)
    with sqlite3.connect(tmp_path / "channels.db") as connection:
        assert connection.execute("SELECT last_update_id FROM provider_accounts").fetchone()[0] == 1
    provider.available = True
    assert gateway.poll(account_id)[0].status == "accepted"


def test_outbound_is_draft_first_strongly_confirmed_and_revocable(system):
    _clock, identity, person, provider, gateway, account_id, _ = system
    pair(system)
    draft = gateway.create_outbound_draft(
        person_id=person["person_id"], provider_account_id=account_id, chat_id="101", text="Hello", purpose="reply"
    )
    assert provider.sent == []
    with pytest.raises(ChannelDenied):
        gateway.confirm_outbound_draft(
            person_id=person["person_id"], draft_id=draft["draft_id"], assurance="low", confirmed=True
        )
    sent = gateway.confirm_outbound_draft(
        person_id=person["person_id"], draft_id=draft["draft_id"], assurance="passkey", confirmed=True
    )
    assert sent["status"] == "sent" and provider.sent[0]["text"] == "Hello"
    binding = identity.resolve_channel_binding(
        provider="telegram", provider_account_id=account_id, external_subject="101"
    )
    assert identity.revoke_paired_channel(
        channel_identity_id=binding["channel_identity_id"], person_id=person["person_id"]
    )
    provider.updates = [telegram_update(2, 101, 1_800_000_000, "after revoke")]
    assert gateway.poll(account_id)[0].status == "unbound-denied"
    assert gateway.revoke_account(person_id=person["person_id"], provider_account_id=account_id)
    with pytest.raises(ChannelDenied):
        gateway.poll(account_id)
