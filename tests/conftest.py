"""Point every path at a temp dir before the app is imported."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="fmk-test-"))
os.environ["FMK_DB_PATH"] = str(_TMP / "test.db")
os.environ["FMK_OUTPUT_DIR"] = str(_TMP / "output")
os.environ["FMK_RECIPES_DIR"] = str(_TMP / "recipes")
os.environ["WS_PORT"] = "19223"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
