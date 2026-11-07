"""Slugs and output paths."""
import re
import unicodedata
from pathlib import Path

from agent import config


def slugify(text: str, max_len: int = 60) -> str:
    """ASCII slug. Vietnamese diacritics fold to plain letters (đ → d)."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return (text[:max_len].rstrip("_")) or "untitled"


def project_dir(project_slug: str) -> Path:
    path = config.OUTPUT_DIR / project_slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def songs_dir(project_slug: str) -> Path:
    path = project_dir(project_slug) / "songs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def videos_dir(project_slug: str) -> Path:
    path = project_dir(project_slug) / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path
