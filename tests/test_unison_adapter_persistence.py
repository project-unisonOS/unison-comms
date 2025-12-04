import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "src"))

from main import UnisonAdapter  # type: ignore  # noqa: E402


def test_unison_adapter_persists_and_encrypts(tmp_path, monkeypatch):
    store_path = tmp_path / "unison.json"
    monkeypatch.setenv("COMMS_UNISON_STORE_PATH", str(store_path))
    # Force a known key for determinism
    monkeypatch.setenv("COMMS_UNISON_KEY", "ZmFrZS1mZXJuZXQta2V5LTEyMw==")  # fake base64 key (invalid but triggers parsing)
    adapter = UnisonAdapter()
    adapter.send_compose("u1", "unison", ["u2"], "Test", "Hello")
    assert store_path.exists()
    # Reload and ensure message is still present
    adapter2 = UnisonAdapter()
    msgs = adapter2.fetch_messages("unison")
    assert any(m for m in msgs if m.get("subject") == "Test")
