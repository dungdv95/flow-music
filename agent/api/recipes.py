"""Captures (recorded UI requests) and recipes (templated captures)."""
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.db import crud
from agent.services import recipes as rc
from agent.services.bridge import BridgeError, get_bridge
from agent.services.music import MusicError, run_recipe

captures_router = APIRouter(prefix="/captures", tags=["captures"])
recipes_router = APIRouter(prefix="/recipes", tags=["recipes"])

# The CSRF token rides in batchexecute bodies; captures never keep it.
_AT_RE = re.compile(r"(^|&)at=[^&]*")


def redact_body(body: Optional[str]) -> Optional[str]:
    if not body:
        return body
    return _AT_RE.sub(r"\1at=REDACTED", body)


# ─── Captures ────────────────────────────────────────────────


class CaptureMode(BaseModel):
    enabled: bool


@captures_router.post("/mode")
async def set_capture_mode(body: CaptureMode):
    try:
        reply = await get_bridge().send("set_capture", {"enabled": body.enabled}, timeout=15)
    except BridgeError as e:
        raise HTTPException(503, str(e))
    if reply.get("error"):
        raise HTTPException(502, reply["error"])
    return {"enabled": body.enabled, "extension": reply.get("data")}


@captures_router.get("")
async def list_captures(q: Optional[str] = None, limit: int = 100):
    rows = await crud.list_rows(
        "capture", limit=limit,
        columns="id, created_at, method, url, status_code, request_content_type, "
                "length(request_body) AS request_len, length(response_text) AS response_len, "
                "substr(request_body, 1, 300) AS request_preview, note")
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["url"] or "").lower()
                or ql in (r.get("request_preview") or "").lower()]
    for r in rows:
        if "/batchexecute" in (r["url"] or ""):
            m = re.search(r"rpcids=([^&]+)", r["url"])
            r["rpcids"] = m.group(1) if m else None
    return rows


@captures_router.get("/{cid}")
async def get_capture(cid: str, full: bool = False):
    row = await crud.get("capture", cid)
    if not row:
        raise HTTPException(404, "capture not found")
    if "/batchexecute" in row["url"] and row.get("request_body"):
        try:
            row["parsed_rpc"] = rc.parse_batch_capture(row["url"], row["request_body"])
        except rc.RecipeError as e:
            row["parsed_rpc"] = {"error": str(e)}
    row["extracted"] = rc.extract(None, row.get("response_text") or "")
    if not full and row.get("response_text") and len(row["response_text"]) > 6000:
        row["response_text"] = row["response_text"][:6000] + f"… [{len(row['response_text'])} chars, ?full=true]"
    return row


@captures_router.delete("/{cid}")
async def delete_capture(cid: str):
    if not await crud.delete("capture", cid):
        raise HTTPException(404, "capture not found")
    return {"ok": True}


@captures_router.delete("")
async def clear_captures():
    return {"deleted": await crud.delete_all("capture")}


# ─── Recipes ─────────────────────────────────────────────────


class FromCaptureIn(BaseModel):
    capture_id: str
    name: str
    sample_values: dict[str, Any] = {}
    description: str = ""
    captcha_action: Optional[str] = None
    extract: dict[str, dict] = {}
    poll: Optional[str] = None
    rpc_index: int = 0
    overwrite: bool = False


class RunIn(BaseModel):
    variables: dict[str, Any] = {}


@recipes_router.get("")
async def list_recipes():
    have = {r["name"] for r in rc.list_recipes() if r.get("valid")}
    return {
        "recipes": rc.list_recipes(),
        "known": [{"name": n, "purpose": d, "recorded": n in have} for n, d in rc.KNOWN_RECIPES.items()],
    }


@recipes_router.get("/{name}")
async def get_recipe(name: str):
    try:
        return rc.load_recipe(name).to_dict()
    except rc.RecipeError as e:
        raise HTTPException(404, str(e))


@recipes_router.put("/{name}")
async def put_recipe(name: str, body: dict):
    try:
        recipe = rc.Recipe.from_dict({**body, "name": name})
        path = rc.save_recipe(recipe)
    except rc.RecipeError as e:
        raise HTTPException(400, str(e))
    return {"saved": str(path), "recipe": recipe.to_dict()}


@recipes_router.delete("/{name}")
async def delete_recipe(name: str):
    path = rc.recipe_path(name)
    if not path.exists():
        raise HTTPException(404, "recipe not found")
    path.unlink()
    return {"ok": True}


@recipes_router.post("/from-capture")
async def recipe_from_capture(body: FromCaptureIn):
    capture = await crud.get("capture", body.capture_id)
    if not capture:
        raise HTTPException(404, "capture not found")
    if rc.has_recipe(body.name) and not body.overwrite:
        raise HTTPException(409, f"recipe '{body.name}' exists — pass overwrite=true")
    try:
        recipe, notes = rc.recipe_from_capture(
            capture, body.name, body.sample_values,
            description=body.description, captcha_action=body.captcha_action,
            extract_rules=body.extract, poll=body.poll, rpc_index=body.rpc_index,
        )
        path = rc.save_recipe(recipe)
    except rc.RecipeError as e:
        raise HTTPException(400, str(e))
    # Show what the extractors pull from the captured response right away.
    preview = rc.extract(recipe, capture.get("response_text") or "")
    return {"saved": str(path), "recipe": recipe.to_dict(), "notes": notes,
            "extract_preview_on_capture": preview}


@recipes_router.post("/{name}/render")
async def render_recipe(name: str, body: RunIn):
    """Dry run: show the exact command that would be sent. Sends nothing."""
    try:
        return rc.load_recipe(name).build_command(body.variables)
    except rc.RecipeError as e:
        raise HTTPException(400, str(e))


@recipes_router.post("/{name}/test-extract")
async def test_extract(name: str, body: dict):
    """Run a recipe's extractors over a capture's response (``capture_id``) or raw ``text``."""
    try:
        recipe = rc.load_recipe(name)
    except rc.RecipeError as e:
        raise HTTPException(404, str(e))
    text = body.get("text")
    if body.get("capture_id"):
        cap = await crud.get("capture", body["capture_id"])
        if not cap:
            raise HTTPException(404, "capture not found")
        text = cap.get("response_text")
    return rc.extract(recipe, text or "")


@recipes_router.post("/{name}/run")
async def run_recipe_now(name: str, body: RunIn):
    """Execute once, synchronously, in the Flow Music tab. Nothing is saved."""
    try:
        return await run_recipe(name, body.variables)
    except rc.RecipeError as e:
        raise HTTPException(400, str(e))
    except MusicError as e:
        raise HTTPException(502, str(e))
