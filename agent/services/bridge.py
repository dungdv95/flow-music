"""Bridge to the Chrome extension.

The agent runs a WebSocket server; the extension connects as a client. The
agent sends commands over the socket ({id, method, params}); the extension runs
them inside a signed-in flowmusic.google tab and posts the answer back to
``POST /api/ext/callback`` (HTTP survives a socket blip mid-generation).

Commands:
    page_fetch    — fetch() inside the page (cookies + same origin)
    batch_rpc     — Google batchexecute POST inside the page (at / f.sid / bl)
    page_download — fetch a media URL inside the page, return base64
    set_capture   — turn request capture on/off
    get_status    — extension + tab status
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any, Optional

from agent import config

logger = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self) -> None:
        self.callback_secret = secrets.token_urlsafe(32)
        self._ws = None
        self._info: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._gen_slots = asyncio.Semaphore(config.GENERATION_MAX_CONCURRENT)
        self._gen_gate = asyncio.Lock()
        self._last_gen_at = 0.0
        self._cooldown_until = 0.0
        self.connected_at: Optional[float] = None

    # ── connection ──────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._ws is not None

    def attach(self, ws) -> None:
        self._ws = ws
        self._info = {}
        self.connected_at = time.time()

    def detach(self, ws) -> None:
        if self._ws is not ws:
            return
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("EXTENSION_DISCONNECTED"))
        self._pending.clear()

    def status(self) -> dict:
        return {
            "extension_connected": self.connected,
            "extension": self._info,
            "pending": len(self._pending),
            "cooldown_s": max(0, round(self._cooldown_until - time.time())),
        }

    async def handle_ws_message(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "extension_ready":
            self._info = {k: v for k, v in data.items() if k != "type"}
            logger.info("Extension ready: %s", self._info)
        elif kind == "ping" and self._ws is not None:
            await self._ws.send(json.dumps({"type": "pong"}))
        elif "id" in data:
            self.resolve(data)

    def resolve(self, data: dict) -> bool:
        fut = self._pending.get(str(data.get("id")))
        if fut is None or fut.done():
            return False
        fut.set_result(data)
        return True

    # ── commands ────────────────────────────────────────────
    async def send(self, method: str, params: Optional[dict] = None,
                   timeout: Optional[float] = None) -> dict:
        if self._ws is None:
            raise BridgeError("EXTENSION_NOT_CONNECTED — open Chrome with the Flow Music Kit extension loaded")
        req_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout or config.BRIDGE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise BridgeError(f"BRIDGE_TIMEOUT after {timeout or config.BRIDGE_TIMEOUT_S}s ({method})") from e
        finally:
            self._pending.pop(req_id, None)

    async def run(self, command: dict, timeout: Optional[float] = None) -> dict:
        """Run a recipe command. Captcha-bearing calls go through the guard."""
        params = command["params"]
        if not params.get("captchaAction"):
            return await self.send(command["method"], params, timeout)

        async with self._gen_slots:
            async with self._gen_gate:
                now = time.time()
                if now < self._cooldown_until:
                    raise BridgeError(
                        f"COOLDOWN — Flow Music flagged unusual activity; "
                        f"retry in {int(self._cooldown_until - now)}s"
                    )
                wait = self._last_gen_at + config.GENERATION_MIN_INTERVAL_S - now
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_gen_at = time.time()
            reply = await self.send(command["method"], params, timeout)
        blob = f"{reply.get('error') or ''} {str(reply.get('data') or '')[:4000]}"
        if "UNUSUAL_ACTIVITY" in blob:
            self._cooldown_until = time.time() + config.UNUSUAL_ACTIVITY_COOLDOWN_S
            logger.warning("Unusual activity flagged — cooling down %ss", config.UNUSUAL_ACTIVITY_COOLDOWN_S)
        return reply


_bridge: Optional[Bridge] = None


def get_bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge
