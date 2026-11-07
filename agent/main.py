"""Flow Music Kit — FastAPI + WebSocket server entry point."""
import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from typing import Optional

import websockets
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import config
from agent.api.music_videos import router as music_videos_router
from agent.api.flowmusic import router as flowmusic_router
from agent.api.projects import router as projects_router
from agent.api.recipes import captures_router, recipes_router, redact_body
from agent.api.requests import router as requests_router
from agent.api.songs import router as songs_router
from agent.db import crud
from agent.db.schema import close_db, init_db
from agent.services import recipes as rc
from agent.services.bridge import get_bridge
from agent.worker.processor import get_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("flow-music-kit")

VERSION = "0.1.0"


# ─── WebSocket server (the extension connects here) ─────────

async def ws_handler(websocket):
    bridge = get_bridge()
    bridge.attach(websocket)
    logger.info("Extension connected")
    await websocket.send(json.dumps({"type": "callback_secret", "secret": bridge.callback_secret,
                                     "apiPort": config.API_PORT}))
    try:
        async for raw in websocket:
            try:
                await bridge.handle_ws_message(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from extension")
            except Exception:
                logger.exception("Error handling extension message")
    except websockets.ConnectionClosed:
        pass
    finally:
        bridge.detach(websocket)
        logger.info("Extension disconnected")


async def run_ws_server():
    async with websockets.serve(ws_handler, config.WS_HOST, config.WS_PORT, max_size=64 * 1024 * 1024):
        logger.info("WebSocket listening on ws://%s:%d", config.WS_HOST, config.WS_PORT)
        await asyncio.Future()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rc.recipes_dir()
    worker = get_worker()
    ws_task = asyncio.create_task(run_ws_server())
    worker_task = asyncio.create_task(worker.run())
    logger.info("Flow Music Kit %s on http://%s:%d", VERSION, config.API_HOST, config.API_PORT)
    yield
    worker.stop()
    ws_task.cancel()
    worker_task.cancel()
    await close_db()


app = FastAPI(title="Flow Music Kit", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(http://(127\.0\.0\.1|localhost)(:\d+)?|chrome-extension://.*)$",
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (projects_router, songs_router, requests_router, captures_router,
               recipes_router, music_videos_router, flowmusic_router):
    app.include_router(router, prefix="/api")


# ─── Extension → agent ───────────────────────────────────────

def _check_secret(secret: str) -> None:
    if not secret or secret != get_bridge().callback_secret:
        raise HTTPException(403, "bad callback secret")


@app.post("/api/ext/callback")
async def ext_callback(request: Request, x_callback_secret: str = Header("")):
    """Command results from the extension (HTTP, so a WS blip loses nothing)."""
    _check_secret(x_callback_secret)
    data = await request.json()
    return {"ok": get_bridge().resolve(data)}


@app.post("/api/ext/capture")
async def ext_capture(request: Request, x_callback_secret: str = Header("")):
    """A request recorded in capture mode. Bodies only — never headers/cookies."""
    _check_secret(x_callback_secret)
    d = await request.json()
    response_text = d.get("responseText") or ""
    if len(response_text) > config.CAPTURE_MAX_RESPONSE_CHARS:
        response_text = response_text[: config.CAPTURE_MAX_RESPONSE_CHARS]
    row = await crud.insert("capture", {
        "page_url": d.get("pageUrl"),
        "url": d.get("url") or "",
        "method": (d.get("method") or "GET").upper(),
        "status_code": d.get("status"),
        "request_content_type": d.get("requestContentType"),
        "request_body": redact_body(d.get("requestBody")),
        "response_content_type": d.get("responseContentType"),
        "response_text": response_text,
    })
    return {"ok": True, "id": row["id"]}


# ─── Settings ────────────────────────────────────────────────

class SettingsIn(BaseModel):
    flow_music_project_id: Optional[str] = None


@app.get("/api/settings")
async def get_settings():
    return {**config.load_settings(), "flow_music_project_id": config.default_flow_project_id()}


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    values = body.model_dump(exclude_unset=True)
    pid = values.get("flow_music_project_id")
    if pid and not rc.UUID_RE.fullmatch(pid):
        raise HTTPException(400, "flow_music_project_id must be a UUID")
    return config.save_settings(values)


# ─── Health / status ─────────────────────────────────────────

@app.get("/health")
async def health():
    bridge = get_bridge()
    return {"status": "ok", "version": VERSION, "extension_connected": bridge.connected}


@app.get("/api/status")
async def status():
    """Everything /fm-doctor needs in one call."""
    bridge = get_bridge()
    recipes = {r["name"]: r for r in rc.list_recipes()}
    counts: dict[str, int] = {}
    for r in await crud.list_rows("request", limit=10000, columns="status"):
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    tab = None
    if bridge.connected:
        try:
            reply = await bridge.send("get_status", {}, timeout=10)
            tab = reply.get("data")
        except Exception as e:  # status must never fail
            tab = {"error": str(e)}
    return {
        "version": VERSION,
        **bridge.status(),
        "flow_music_tab": tab,
        "flow_music_project_id": config.default_flow_project_id() or None,
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "recipes": {
            name: ("ok" if recipes.get(name, {}).get("valid") else
                   "invalid" if name in recipes else "missing")
            for name in rc.KNOWN_RECIPES
        },
        "custom_recipes": sorted(set(recipes) - set(rc.KNOWN_RECIPES)),
        "requests": counts,
        "worker_active": get_worker().active_count,
        "paths": {"db": str(config.DB_PATH), "output": str(config.OUTPUT_DIR),
                  "recipes": str(config.RECIPES_DIR)},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("agent.main:app", host=config.API_HOST, port=config.API_PORT)
