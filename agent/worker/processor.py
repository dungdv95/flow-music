"""Background worker: PENDING → submit → (POLLING →) download → COMPLETED.

Each request names the recipe used for its submit. When the submit response
already holds audio URLs the job finishes right away; otherwise the worker
takes the ``job_id`` it extracted and polls with the recipe's ``poll`` recipe
(default ``poll_song``) until audio URLs appear, an extracted ``failed`` value
turns truthy, or POLL_TIMEOUT_S passes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from agent import config
from agent.db import crud
from agent.services import recipes as rc
from agent.services.music import MusicError, download, run_recipe
from agent.services.post_process import FFmpegError, probe_duration
from agent.utils.paths import songs_dir, videos_dir

logger = logging.getLogger(__name__)

MAX_ACTIVE = 4
MAX_TAKES = 4
SONG_TYPES = {"GENERATE_SONG", "EXTEND_SONG", "COVER_SONG", "RUN_RECIPE"}
# Failures worth another try: the browser side was briefly unavailable.
TRANSIENT = ("BRIDGE_TIMEOUT", "EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED",
             "NO_FLOW_MUSIC_TAB", "TAB_DISCARDED", "CAPTCHA", "NO_INJECTION_RESULT",
             "COOLDOWN", "Failed to fetch")


def _parse_ts(value: Optional[str]) -> float:
    if not value:
        return time.time()
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Worker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._active: dict[str, asyncio.Task] = {}
        self._last_poll: dict[str, float] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Anything left PROCESSING by a crash goes back to the queue.
        for req in await crud.list_rows("request", {"status": "PROCESSING"}):
            await crud.update("request", req["id"], {"status": "PENDING"})
        logger.info("Worker started")
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), config.WORKER_TICK_S)
            except asyncio.TimeoutError:
                pass
        for task in list(self._active.values()):
            task.cancel()

    async def _tick(self) -> None:
        for rid, task in list(self._active.items()):
            if task.done():
                self._active.pop(rid, None)
        now = time.time()
        polling = await crud.list_rows("request", {"status": "POLLING"}, order="created_at ASC")
        for req in polling:
            if len(self._active) >= MAX_ACTIVE:
                return
            if req["id"] in self._active:
                continue
            if now - self._last_poll.get(req["id"], 0) < config.POLL_INTERVAL_S:
                continue
            self._spawn(req["id"], self._poll(req))
        pending = await crud.list_rows("request", {"status": "PENDING"}, order="created_at ASC", limit=10)
        for req in pending:
            if len(self._active) >= MAX_ACTIVE:
                return
            if req["id"] not in self._active:
                self._spawn(req["id"], self._submit(req))

    def _spawn(self, rid: str, coro) -> None:
        self._active[rid] = asyncio.create_task(coro)

    # ── stages ──────────────────────────────────────────────
    async def _submit(self, req: dict) -> None:
        rid = req["id"]
        await crud.update("request", rid, {"status": "PROCESSING", "submitted_at": _now(),
                                           "error_message": None})
        if req.get("song_id"):
            await crud.update("song", req["song_id"], {"status": "PROCESSING", "error_message": None})
        try:
            recipe = rc.load_recipe(req["recipe"])
            out = await run_recipe(req["recipe"], req.get("variables") or {}, recipe)
            await self._after_response(req, recipe, out["extracted"], from_poll=False)
        except rc.RecipeError as e:
            await self._fail(req, f"RECIPE: {e}", retry=False)
        except (MusicError, FFmpegError) as e:
            await self._fail(req, str(e), retry=True)
        except Exception as e:  # keep the worker alive whatever happens
            logger.exception("Submit %s crashed", rid)
            await self._fail(req, f"INTERNAL: {e}", retry=False)

    async def _poll(self, req: dict) -> None:
        rid = req["id"]
        self._last_poll[rid] = time.time()
        if time.time() - _parse_ts(req.get("submitted_at")) > config.POLL_TIMEOUT_S:
            await self._fail(req, f"POLL_TIMEOUT after {int(config.POLL_TIMEOUT_S)}s", retry=False)
            return
        try:
            submit_recipe = rc.load_recipe(req["recipe"])
            poll_name = submit_recipe.poll or "poll_song"
            poll_recipe = rc.load_recipe(poll_name)
            variables = {**(req.get("variables") or {}), "job_id": req["job_id"]}
            out = await run_recipe(poll_name, variables, poll_recipe)
            await crud.update("request", rid, {"poll_count": (req.get("poll_count") or 0) + 1})
            await self._after_response(req, poll_recipe, out["extracted"], from_poll=True)
        except rc.RecipeError as e:
            await self._fail(req, f"RECIPE: {e}", retry=False)
        except MusicError as e:
            # A failed poll is not a failed job; keep polling until timeout
            # unless Flow reported a hard error code.
            if any(code in str(e) for code in ("PUBLIC_ERROR", "PERMISSION_DENIED", "UNAUTHENTICATED")):
                await self._fail(req, str(e), retry=False)
            else:
                logger.info("Poll %s: %s (will retry)", rid[:8], e)
        except Exception as e:
            logger.exception("Poll %s crashed", rid)
            await self._fail(req, f"INTERNAL: {e}", retry=False)

    async def _after_response(self, req: dict, recipe: rc.Recipe, ex: dict, from_poll: bool) -> None:
        rid, kind = req["id"], req["type"]

        if kind == "GENERATE_LYRICS":
            lyrics = ex.get("lyrics")
            if isinstance(lyrics, list):
                lyrics = "\n".join(str(x) for x in lyrics)
            if lyrics:
                if req.get("song_id"):
                    await crud.update("song", req["song_id"], {"lyrics": lyrics, "status": "DRAFT"})
                await self._complete(req, {"lyrics": lyrics})
                return
        elif kind == "UPLOAD_AUDIO":
            remote_id = ex.get("remote_id") or ex.get("job_id")
            if remote_id:
                if req.get("song_id"):
                    await crud.update("song", req["song_id"], {"remote_id": remote_id, "status": "COMPLETED"})
                await self._complete(req, {"remote_id": remote_id})
                return
        elif kind == "GENERATE_MUSIC_VIDEO" and ex.get("video_urls"):
            await self._finish_native_video(req, ex)
            return
        elif kind in SONG_TYPES and ex.get("audio_urls"):
            await self._finish_song(req, ex)
            return

        if ex.get("failed"):
            await self._fail(req, f"FLOW_MUSIC_FAILED: {ex.get('failed')}", retry=False)
            return

        job_id = ex.get("job_id") if not from_poll else req.get("job_id")
        if job_id and (recipe.poll or rc.has_recipe("poll_song") or from_poll):
            if not from_poll:
                await crud.update("request", rid, {"status": "POLLING", "job_id": str(job_id),
                                                   "result": _slim(ex)})
                if req.get("song_id") and kind in SONG_TYPES:
                    await crud.update("song", req["song_id"], {"remote_id": ex.get("remote_id") or str(job_id)})
            return

        if kind == "RUN_RECIPE":
            await self._complete(req, _slim(ex))
            return
        await self._fail(
            req,
            "NO_OUTPUT: response had no audio URL and no job_id — add extract rules "
            f"to recipe '{recipe.name}' (see /fm-capture)",
            retry=False,
        )

    async def _finish_song(self, req: dict, ex: dict) -> None:
        song = await crud.get("song", req["song_id"]) if req.get("song_id") else None
        result = _slim(ex)
        if song is None:
            await self._complete(req, result)
            return
        project = await crud.get("project", song["project_id"])
        folder = songs_dir(project["slug"])
        takes = []
        for i, url in enumerate(ex["audio_urls"][:MAX_TAKES]):
            path = await download(url, folder / f"{song['slug']}_{i + 1}")
            take = {"url": url, "path": str(path)}
            try:
                take["duration_s"] = round(await probe_duration(path), 2)
            except FFmpegError:
                pass
            takes.append(take)
        updates = {
            "takes": takes,
            "selected_take": 0,
            "audio_path": takes[0]["path"],
            "audio_duration_s": takes[0].get("duration_s"),
            "status": "COMPLETED",
            "error_message": None,
        }
        if ex.get("remote_id") or req.get("job_id"):
            updates["remote_id"] = ex.get("remote_id") or req.get("job_id")
        if ex.get("lyrics") and not song.get("lyrics"):
            updates["lyrics"] = ex["lyrics"]
        if ex.get("image_urls"):
            try:
                cover = await download(ex["image_urls"][0], folder / f"{song['slug']}_cover")
                updates["cover_path"] = str(cover)
            except MusicError as e:
                logger.info("Cover download skipped: %s", e)
        await crud.update("song", song["id"], updates)
        await self._complete(req, {**result, "takes": takes})

    async def _finish_native_video(self, req: dict, ex: dict) -> None:
        mv_id = (req.get("variables") or {}).get("music_video_id")
        mv = await crud.get("music_video", mv_id) if mv_id else None
        if mv is None:
            await self._complete(req, _slim(ex))
            return
        project = await crud.get("project", mv["project_id"])
        path = await download(ex["video_urls"][0], videos_dir(project["slug"]) / f"mv_{mv['id'][:8]}_native")
        await crud.update("music_video", mv["id"], {"status": "COMPLETED", "output_path": str(path)})
        await self._complete(req, {**_slim(ex), "output_path": str(path)})

    async def _complete(self, req: dict, result: dict) -> None:
        self._last_poll.pop(req["id"], None)
        await crud.update("request", req["id"], {"status": "COMPLETED", "result": result,
                                                 "error_message": None})
        logger.info("Request %s %s completed", req["id"][:8], req["type"])

    async def _fail(self, req: dict, message: str, retry: bool) -> None:
        self._last_poll.pop(req["id"], None)
        retries = req.get("retry_count") or 0
        if retry and retries < config.MAX_RETRIES and any(t in message for t in TRANSIENT):
            await crud.update("request", req["id"], {"status": "PENDING", "retry_count": retries + 1,
                                                     "error_message": message})
            logger.warning("Request %s retry %d: %s", req["id"][:8], retries + 1, message)
            return
        await crud.update("request", req["id"], {"status": "FAILED", "error_message": message})
        if req.get("song_id") and req["type"] in SONG_TYPES | {"GENERATE_LYRICS", "UPLOAD_AUDIO"}:
            await crud.update("song", req["song_id"], {"status": "FAILED", "error_message": message})
        mv_id = (req.get("variables") or {}).get("music_video_id")
        if mv_id:
            await crud.update("music_video", mv_id, {"status": "FAILED", "error_message": message})
        logger.warning("Request %s failed: %s", req["id"][:8], message)


def _slim(ex: dict) -> dict:
    """Drop empty heuristic keys so stored results stay readable."""
    return {k: v for k, v in ex.items() if v not in (None, [], "", {})}


_worker: Optional[Worker] = None


def get_worker() -> Worker:
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker
