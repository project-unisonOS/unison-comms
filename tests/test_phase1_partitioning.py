from types import SimpleNamespace

import main


def test_comms_paths_and_keys_are_partitioned_by_principal(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_GMAIL_STORE_PATH", str(tmp_path / "gmail.enc"))
    monkeypatch.setenv("COMMS_ROOT_KEY", "phase1-comms-root-key-material-32-bytes")
    alice = SimpleNamespace(
        credential_namespace="credential-alice",
        data_namespace="data-alice",
        key_handle="key-alice",
    )
    bob = SimpleNamespace(
        credential_namespace="credential-bob",
        data_namespace="data-bob",
        key_handle="key-bob",
    )

    monkeypatch.setattr(main, "get_current_principal", lambda: alice)
    alice_path = main._gmail_store_path()
    alice_key = main._gmail_store_key()
    monkeypatch.setattr(main, "get_current_principal", lambda: bob)
    bob_path = main._gmail_store_path()
    bob_key = main._gmail_store_key()

    assert alice_path != bob_path
    assert alice_key != bob_key
    assert "alice" not in str(alice_path)
    assert "bob" not in str(bob_path)
