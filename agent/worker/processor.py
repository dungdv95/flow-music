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
from agent.services.flowmusic_api import FlowMusicError, get_flowmusic_api
from agent.services.music import MusicError, download, run_recipe
from agent.services.post_process import FFmpegError, probe_duration
from agent.utils.paths import songs_dir, videos_dir

logger = logging.getLogger(__name__)

MAX_ACTIVE = 4
MAX_TAKES = 4
SONG_TYPES = {"GENERATE_SONG", "EXTEND_SONG", "COVER_SONG", "RUN_RECIPE"}
# Requests whose recipe is "native:<tool>" call Flow Music's API directly
# (agent/services/flowmusic_api.py) instead of a recorded recipe.
NATIVE_PREFIX = "native:"
MV_TIMEOUT_S = 3600
BROWSER_GONE = ("EXTENSION_NOT_CONNECTED", "EXTENSION_DISCONNECTED", "BRIDGE_TIMEOUT",
                "NO_FLOW_MUSIC_TAB", "TAB_DISCARDED", "NO_INJECTION_RESULT")
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
        self._stop = asyncio.Event()
        self._active.clear()
        # Anything left PROCESSING by a crash goes back to the queue.
        for req in await crud.list_rows("request", {"status": "PROCESSING"}):
            if not req["recipe"].startswith(NATIVE_PREFIX):
                await crud.update("request", req["id"], {"status": "PENDING"})
            elif req.get("result"):
                await crud.update("request", req["id"], {"status": "POLLING"})
            else:
                # It may or may not have reached Flow Music; resubmitting could
                # charge twice, so a human decides.
                await self._fail(req, "INTERRUPTED during submit — check the Flow Music library, "
                                      "then POST /api/requests/<id>/retry if nothing was created", retry=False)
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
        if req.get("song_id") and req["type"] != "GENERATE_MUSIC_VIDEO":
            await crud.update("song", req["song_id"], {"status": "PROCESSING", "error_message": None})
        if req["recipe"].startswith(NATIVE_PREFIX):
            await self._native(req, resume=False)
            return
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
        if req["recipe"].startswith(NATIVE_PREFIX):
            # Reached after a restart or a browser outage mid-job: pick it back up.
            if time.time() - self._last_poll.get(rid, 0) < config.POLL_INTERVAL_S:
                return
            self._last_poll[rid] = time.time()
            await self._native(req, resume=True)
            return
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

    # ── native Flow Music API ───────────────────────────────
    async def _native(self, req: dict, resume: bool) -> None:
        """Submit (or resume) a native job. Never auto-retried: a second
        submit would be charged again."""
        tool = req["recipe"][len(NATIVE_PREFIX):]
        try:
            if tool == "audio__create_song":
                await self._native_song(req, resume)
            elif tool == "video__create_music_video":
                await self._native_video(req, resume)
            else:
                await self._fail(req, f"NATIVE: unsupported tool {tool}", retry=False)
        except (FlowMusicError, MusicError, FFmpegError) as e:
            msg = str(e)
            if any(t in msg for t in BROWSER_GONE):
                # The browser side is briefly unavailable. Once a job is
                # submitted keep watching it; before that nothing was sent, so
                # it is safe to queue again. Never resubmit a submitted job.
                fresh = await crud.get("request", req["id"]) or req
                submitted = fresh.get("status") == "POLLING" or bool((fresh.get("result") or {}))
                await crud.update("request", req["id"], {
                    "status": "POLLING" if submitted else "PENDING", "error_message": msg})
                self._last_poll[req["id"]] = time.time()
                logger.warning("Native %s waiting for the browser: %s", req["id"][:8], msg)
                return
            await self._fail(req, msg, retry=False)
        except Exception as e:
            logger.exception("Native %s crashed", req["id"])
            await self._fail(req, f"INTERNAL: {e}", retry=False)

    async def _native_song(self, req: dict, resume: bool) -> None:
        api = get_flowmusic_api()
        v = req.get("variables") or {}
        result = req.get("result") or {}
        clip_ids = result.get("clip_ids") if resume else None
        if not clip_ids:
            if resume:
                raise FlowMusicError("RESUME_IMPOSSIBLE: no clip ids saved — check the Flow Music library before retrying")
            res = await api.create_song(
                project_id=v.get("flow_project_id"), title=v.get("title") or "Untitled",
                sound_prompt=v.get("prompt") or "", lyrics=v.get("lyrics") or "",
                instrumental=bool(v.get("instrumental")), bpm=v.get("bpm"),
                length_s=v.get("duration_s"), seed=v.get("seed"))
            clip_ids = res["clip_ids"]
            result = {"clip_ids": clip_ids, "conversation_id": res.get("conversation_id")}
            await crud.update("request", req["id"], {"status": "POLLING", "job_id": res["job_id"],
                                                     "result": result})
            if req.get("song_id"):
                await crud.update("song", req["song_id"], {"remote_id": clip_ids[0]})
            req = {**req, "job_id": res["job_id"]}
        clips = await api.wait_for_clips(clip_ids, timeout_s=config.POLL_TIMEOUT_S)
        ordered = [clips[c] for c in clip_ids if c in clips]
        ex = {
            "audio_urls": [c["audio_url"] for c in ordered],
            "image_urls": [c["image_url"] for c in ordered if c.get("image_url")][:1],
            "remote_id": clip_ids[0],
            "take_meta": [{"clip_id": c["id"], "wav_url": c.get("wav_url"),
                           "remote_duration_s": _float((c.get("duration") or {}).get("value"))}
                          for c in ordered],
            **result,
        }
        await self._finish_song(req, ex)

    async def _native_video(self, req: dict, resume: bool) -> None:
        api = get_flowmusic_api()
        v = req.get("variables") or {}
        result = req.get("result") or {}
        mv_job = result.get("mv_job_id")
        if not mv_job:
            if resume:
                raise FlowMusicError("RESUME_IMPOSSIBLE: render job id not saved — check Library → Music videos before retrying")
            res = await api.create_music_video(
                clip_id=v["clip_id"], user_message=v.get("user_message") or "",
                aspect_ratio=v.get("aspect_ratio") or "16:9", resolution=v.get("resolution") or "720p",
                start_s=v.get("start_s") or 0, duration_s=v.get("duration_s"),
                render_lyrics=bool(v.get("render_lyrics")), project_id=v.get("flow_project_id"),
                conversation_id=v.get("conversation_id"))
            mv_job = res["job_id"]
            result = {"mv_job_id": mv_job, "conversation_id": res.get("conversation_id"),
                      "proposal": res.get("proposal")}
            await crud.update("request", req["id"], {"status": "POLLING", "job_id": mv_job, "result": result})
        state = await api.wait_for_music_video(mv_job, timeout_s=MV_TIMEOUT_S)
        result = {**result, "video_url": state["final_video_url"],
                  "token_estimate": state.get("token_estimate"),
                  "total_runtime_s": state.get("total_runtime_s")}
        await self._finish_native_video({**req, "result": result}, {"video_urls": [state["final_video_url"]], **result})

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
        meta = ex.get("take_meta") or []
        for i, url in enumerate(ex["audio_urls"][:MAX_TAKES]):
            path = await download(url, folder / f"{song['slug']}_{i + 1}")
            take = {"url": url, "path": str(path), **(meta[i] if i < len(meta) else {})}
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
        result.pop("take_meta", None)
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


def _float(value) -> Optional[float]:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _slim(ex: dict) -> dict:
    """Drop empty heuristic keys so stored results stay readable."""
    return {k: v for k, v in ex.items() if v not in (None, [], "", {})}


_worker: Optional[Worker] = None


def get_worker() -> Worker:
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker
