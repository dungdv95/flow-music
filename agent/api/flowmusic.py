"""Read-only views of the Flow Music account, through the signed-in tab."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.services.flowmusic_api import FlowMusicError, get_flowmusic_api

router = APIRouter(prefix="/flowmusic", tags=["flowmusic"])


class ClipsIn(BaseModel):
    clip_ids: list[str]


def _http(e: FlowMusicError) -> HTTPException:
    return HTTPException(e.status if e.status and 400 <= e.status < 600 else 502, str(e))


@router.get("/account")
async def account():
    """Who is signed in, credit balance and the default model."""
    api = get_flowmusic_api()
    try:
        me = await api.me()
        credits = await api.credits()
        model = await api.default_model()
    except FlowMusicError as e:
        raise _http(e)
    user = me if isinstance(me, dict) else {}
    return {
        "user": {k: user.get(k) for k in ("id", "username", "display_name", "email") if k in user},
        "credits": credits,
        "default_model": model,
    }


@router.get("/tools")
async def tools(name: Optional[str] = None):
    """Live tool schemas (what /producer/tool-call accepts)."""
    try:
        data = await get_flowmusic_api().tools()
    except FlowMusicError as e:
        raise _http(e)
    items = data.get("tools", data) if isinstance(data, dict) else data
    if name and isinstance(items, list):
        items = [t for t in items if name in str(t.get("name") or t.get("tool_name") or "")]
    elif name and isinstance(items, dict):
        items = {k: v for k, v in items.items() if name in k}
    return items


@router.post("/clips")
async def clips(body: ClipsIn):
    try:
        return await get_flowmusic_api().clips(body.clip_ids)
    except FlowMusicError as e:
        raise _http(e)


@router.get("/raw")
async def raw_get(path: str):
    """GET any /__api path (read-only) — for debugging new endpoints."""
    if not path.startswith("/"):
        raise HTTPException(400, "path must start with /")
    try:
        return await get_flowmusic_api().call("GET", path)
    except FlowMusicError as e:
        raise _http(e)


class ChatIn(BaseModel):
    text: str
    conversation_id: Optional[str] = None


@router.post("/chat")
async def chat(body: ChatIn):
    """Send one message to Flow Music's agent. It may run tools (and spend
    credits) on its own, exactly as in the web app."""
    api = get_flowmusic_api()
    try:
        res = await api.send_message(body.text, conversation_id=body.conversation_id)
        conv = await api.get_conversation(res["conversation_id"]) if res["conversation_id"] else None
    except FlowMusicError as e:
        raise _http(e)
    kinds: dict[str, int] = {}
    for name, _ in res["events"]:
        kinds[name] = kinds.get(name, 0) + 1
    return {"conversation_id": res["conversation_id"], "job_id": res["job_id"], "event_counts": kinds,
            "conversation": conv}
