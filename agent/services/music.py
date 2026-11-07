"""High-level Flow Music operations built on recipes + the bridge."""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

import httpx

from agent.services import recipes as rc
from agent.services.bridge import BridgeError, get_bridge

logger = logging.getLogger(__name__)

MAX_PAGE_DOWNLOAD_BYTES = 60 * 1024 * 1024


class MusicError(RuntimeError):
    pass


async def run_recipe(name: str, variables: Optional[dict] = None,
                     recipe: Optional[rc.Recipe] = None) -> dict:
    """Render + execute one recipe. Returns {status, extracted, text_preview}.

    Raises MusicError on transport failures, HTTP errors and Flow error codes.
    """
    recipe = recipe or rc.load_recipe(name)
    command = recipe.build_command(variables or {})
    try:
        reply = await get_bridge().run(command)
    except BridgeError as e:
        raise MusicError(str(e)) from e

    if reply.get("error"):
        raise MusicError(str(reply["error"]))
    status = reply.get("status")
    text = reply.get("data") or ""
    if not isinstance(text, str):
        import json
        text = json.dumps(text)
    extracted = rc.extract(recipe, text)
    if isinstance(status, int) and status >= 400:
        raise MusicError(f"HTTP_{status} {', '.join(extracted['errors']) or text[:300]}")
    if extracted["errors"]:
        raise MusicError(", ".join(extracted["errors"]))
    return {"status": status, "extracted": extracted, "text_preview": text[:1500]}


def _ext_for(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    guessed = mimetypes.guess_extension(ct) if ct else None
    if ct in ("audio/mpeg", "audio/mp3"):
        guessed = ".mp3"
    if not guessed:
        suffix = Path(url.split("?")[0]).suffix
        guessed = suffix if 1 < len(suffix) <= 6 else ".mp3"
    return guessed


async def download(url: str, dest_stem: Path) -> Path:
    """Download a media URL to ``dest_stem`` + detected extension.

    Signed CDN links download directly. Links that need the browser session
    (401/403, or an HTML login page) are fetched inside the Flow Music tab.
    """
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            resp = await client.get(url)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "text/html" not in ctype and resp.content:
            path = dest_stem.with_suffix(_ext_for(ctype, url))
            path.write_bytes(resp.content)
            return path
        logger.info("Direct download got %s (%s) — retrying in page", resp.status_code, ctype)
    except httpx.HTTPError as e:
        logger.info("Direct download failed (%s) — retrying in page", e)

    try:
        reply = await get_bridge().send(
            "page_download", {"url": url, "maxBytes": MAX_PAGE_DOWNLOAD_BYTES}, timeout=300
        )
    except BridgeError as e:
        raise MusicError(f"DOWNLOAD_FAILED: {e}") from e
    if reply.get("error"):
        raise MusicError(f"DOWNLOAD_FAILED: {reply['error']}")
    data = reply.get("data") or {}
    if not data.get("base64"):
        raise MusicError(f"DOWNLOAD_FAILED: HTTP {data.get('status')}")
    path = dest_stem.with_suffix(_ext_for(data.get("contentType", ""), url))
    path.write_bytes(base64.b64decode(data["base64"]))
    return path


def audio_file_variables(path: Path) -> dict[str, Any]:
    """Variables for the upload_audio recipe."""
    mime = mimetypes.guess_type(str(path))[0] or "audio/mpeg"
    return {
        "audio_b64": base64.b64encode(path.read_bytes()).decode(),
        "filename": path.name,
        "mime_type": mime,
        "size_bytes": path.stat().st_size,
    }
