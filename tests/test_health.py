import pathlib
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "src"))

from main import app  # noqa: E402


def test_health_and_ready():
    client = TestClient(app)
    health = client.get("/health")
    ready = client.get("/readyz")
    assert health.status_code == 200
    assert ready.status_code == 200
    assert health.json().get("service") == "unison-comms"
    assert ready.json().get("status") == "ready"
