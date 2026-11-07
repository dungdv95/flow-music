"""Job queue inspection + generic recipe jobs."""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.db import crud
from agent.services import recipes as rc

router = APIRouter(prefix="/requests", tags=["requests"])


class RecipeJobIn(BaseModel):
    recipe: str
    variables: dict[str, Any] = {}
    project_id: Optional[str] = None
    song_id: Optional[str] = None


@router.get("")
async def list_requests(status: Optional[str] = None, song_id: Optional[str] = None,
                        project_id: Optional[str] = None, limit: int = 50):
    return await crud.list_rows("request", {"status": status, "song_id": song_id,
                                            "project_id": project_id}, limit=limit)


@router.get("/summary")
async def summary():
    rows = await crud.list_rows("request", limit=10000, columns="status")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


@router.post("")
async def create_recipe_job(body: RecipeJobIn):
    """Queue any recipe. Audio URLs in the result are downloaded when song_id is set."""
    if not rc.has_recipe(body.recipe):
        raise HTTPException(409, f"recipe '{body.recipe}' not recorded yet")
    return await crud.insert("request", {
        "type": "RUN_RECIPE", "recipe": body.recipe, "variables": body.variables,
        "project_id": body.project_id, "song_id": body.song_id,
    })


@router.get("/{rid}")
async def get_request(rid: str):
    row = await crud.get("request", rid)
    if not row:
        raise HTTPException(404, "request not found")
    # Uploaded audio is large; don't echo it back.
    if isinstance(row.get("variables"), dict) and "audio_b64" in row["variables"]:
        row["variables"]["audio_b64"] = f"<{len(row['variables']['audio_b64'])} chars>"
    return row


@router.post("/{rid}/retry")
async def retry_request(rid: str):
    row = await crud.get("request", rid)
    if not row:
        raise HTTPException(404, "request not found")
    if row["status"] not in ("FAILED", "COMPLETED"):
        raise HTTPException(409, f"request is {row['status']}")
    if row.get("song_id"):
        await crud.update("song", row["song_id"], {"status": "PROCESSING", "error_message": None})
    return await crud.update("request", rid, {
        "status": "PENDING", "error_message": None, "job_id": None, "result": None,
        "poll_count": 0, "retry_count": 0,
    })


@router.delete("/{rid}")
async def delete_request(rid: str):
    if not await crud.delete("request", rid):
        raise HTTPException(404, "request not found")
    return {"ok": True}
