"""Configuration constants. Every value can be overridden with an env var."""
import json
import os
from pathlib import Path


def _float(name: str, default: str, minimum: float = 0.0) -> float:
    return max(minimum, float(os.environ.get(name, default)))


def _int(name: str, default: str, minimum: int = 0) -> int:
    return max(minimum, int(os.environ.get(name, default)))


# ─── Paths ───────────────────────────────────────────────────
BASE_DIR = Path(os.environ.get("FMK_DIR", Path(__file__).parent.parent))
DB_PATH = Path(os.environ.get("FMK_DB_PATH", BASE_DIR / "flow_music.db"))
OUTPUT_DIR = Path(os.environ.get("FMK_OUTPUT_DIR", BASE_DIR / "output"))
RECIPES_DIR = Path(os.environ.get("FMK_RECIPES_DIR", BASE_DIR / "recipes"))

# ─── Servers ─────────────────────────────────────────────────
# Different ports from Flow Kit (8100 / 9222) so both can run side by side.
# 8200 / 9223 were already taken by another local tool on the dev machine.
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_PORT = _int("API_PORT", "8300", 1)
WS_HOST = os.environ.get("WS_HOST", "127.0.0.1")
WS_PORT = _int("WS_PORT", "9300", 1)

# ─── Flow Music ──────────────────────────────────────────────
FLOW_MUSIC_URL = os.environ.get("FLOW_MUSIC_URL", "https://flowmusic.app/")

# Local, per-machine settings changed through PUT /api/settings (gitignored).
SETTINGS_PATH = Path(os.environ.get("FMK_SETTINGS_PATH", BASE_DIR / "settings.json"))


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> dict:
    merged = {**load_settings(), **values}
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2) + "\n")
    return merged


def default_flow_project_id() -> str:
    """Flow Music project used when a local project doesn't name its own.

    Filled into recipes as {{flow_project_id}}."""
    return os.environ.get("FLOW_MUSIC_PROJECT_ID") or load_settings().get("flow_music_project_id", "")

# Timeout for one command sent to the extension (page fetch, captcha, rpc).
BRIDGE_TIMEOUT_S = _float("BRIDGE_TIMEOUT_S", "120", 5)

# Generation guard: every captcha-bearing call goes through it, so a burst of
# queued songs can't hammer the account.
GENERATION_MIN_INTERVAL_S = _float("GENERATION_MIN_INTERVAL_S", "5")
GENERATION_MAX_CONCURRENT = _int("GENERATION_MAX_CONCURRENT", "1", 1)
UNUSUAL_ACTIVITY_COOLDOWN_S = _float("UNUSUAL_ACTIVITY_COOLDOWN_S", "180")

# ─── Worker ──────────────────────────────────────────────────
WORKER_TICK_S = _float("WORKER_TICK_S", "3", 0.5)
POLL_INTERVAL_S = _float("POLL_INTERVAL_S", "8", 1)
POLL_TIMEOUT_S = _float("POLL_TIMEOUT_S", "600", 30)
MAX_RETRIES = _int("MAX_RETRIES", "2")

# ─── Captures ────────────────────────────────────────────────
CAPTURE_MAX_RESPONSE_CHARS = _int("CAPTURE_MAX_RESPONSE_CHARS", "2000000", 1000)

# ─── Flow Kit (optional — visuals for music videos) ─────────
FLOWKIT_URL = os.environ.get("FLOWKIT_URL", "http://127.0.0.1:8100")
FLOWKIT_DIR = Path(os.environ.get(
    "FLOWKIT_DIR", BASE_DIR.parent / "flowkit"
))

# ─── Music video defaults ───────────────────────────────────
DEFAULT_CLIP_SECONDS = _float("DEFAULT_CLIP_SECONDS", "8", 1)
