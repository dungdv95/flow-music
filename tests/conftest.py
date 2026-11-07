"""Point every path at a temp dir before the app is imported."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="fmk-test-"))
os.environ["FMK_DB_PATH"] = str(_TMP / "test.db")
os.environ["FMK_OUTPUT_DIR"] = str(_TMP / "output")
os.environ["FMK_RECIPES_DIR"] = str(_TMP / "recipes")
os.environ["FMK_SETTINGS_PATH"] = str(_TMP / "settings.json")
os.environ["WS_PORT"] = "19223"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from agent import config
    from agent.main import app
    monkeypatch.setattr(config, "WORKER_TICK_S", 0.5)
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 1)
    monkeypatch.setattr(config, "GENERATION_MIN_INTERVAL_S", 0)
    with TestClient(app) as c:
        yield c
