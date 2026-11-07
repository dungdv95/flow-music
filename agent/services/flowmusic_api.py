"""Direct client for Flow Music's own web API (flowmusic.app/__api).

Flow Music is the former Producer.ai app. Everything it generates goes through
one endpoint: the client posts a *tool call* (``audio__create_song``,
``video__create_music_video``, …) to ``/producer/tool-call``, gets a ``job_id``
back, and reads the result from the server-sent-event stream
``/messages/<job_id>/stream``. Finished media lives on clips (``POST /clips``).

The calls are made inside the signed-in tab by the extension (``page_api``),
which attaches the page's own Supabase session as the bearer token — this
module never sees or stores credentials.

Shapes below were read from the app's public JS bundle (build of 2026-11):
- ``audio__create_song`` args: ``{sound_prompt, lyrics_id, title, seed, image_id}``;
  the lyrics text travels in ``client_context.lyrics_id_map[lyrics_id]``,
  ``lyrics_id = uuid5(LYRICS_NAMESPACE, lyrics)``; BPM is appended to the
  prompt (``", 112 bpm"``) and length to the lyrics (``"[End - 2:00]"``).
- ``video__create_music_video`` args: ``{inputs: {clip_id, user_message,
  style_image_url, likeness_image_url, aspect_ratio, resolution,
  render_lyrics, start_s, duration_s}}``; cost ≈ ceil(duration_s × 12.5) credits.
``GET /producer/tools`` returns the live schemas — check it when a call breaks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Any, Optional

from agent import config
from agent.services.bridge import BridgeError, get_bridge

logger = logging.getLogger(__name__)

LYRICS_NAMESPACE = uuid.UUID("b8f9e3a1-7c2d-4f5e-9a8b-1c3d5e7f9a2b")
INSTRUMENTAL_LYRICS = "[Instrumental]"
MV_CREDITS_PER_SECOND = 12.5

_gen_lock = asyncio.Lock()
_last_gen_at = 0.0


class FlowMusicError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def lyrics_id_for(lyrics: str) -> str:
    """Same id the web app derives for a lyrics text (uuid v5)."""
    if not lyrics or not lyrics.strip() or lyrics == INSTRUMENTAL_LYRICS:
        return ""
    return str(uuid.uuid5(LYRICS_NAMESPACE, lyrics))


def length_tag(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def build_create_song_args(*, title: str, sound_prompt: str, lyrics: str = "",
                           instrumental: bool = False, bpm: Optional[int] = None,
                           length_s: Optional[float] = None, seed: Optional[int] = None,
                           image_id: Optional[str] = None) -> tuple[dict, dict]:
    """Return (tool args, lyrics_id_map) exactly like the web compose form."""
    text = INSTRUMENTAL_LYRICS if instrumental else (lyrics or "")
    if length_s:
        tag = f"[End - {length_tag(length_s)}]"
        text = f"{text}\n{tag}" if text else tag
    prompt = (sound_prompt or "").strip()
    if bpm:
        prompt = f"{prompt}, {bpm} bpm" if prompt else f"{bpm} bpm"
    lid = lyrics_id_for(text)
    args = {"sound_prompt": prompt, "lyrics_id": lid, "title": title or "Untitled",
            "seed": seed, "image_id": image_id}
    return args, ({lid: text} if lid else {})


def music_video_credits(duration_s: float) -> int:
    return 0 if not duration_s or duration_s <= 0 else math.ceil(duration_s * MV_CREDITS_PER_SECOND)


def parse_sse(text: str) -> list[tuple[str, Any]]:
    """Parse an event-stream body into [(event, data)]."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        events.append((name, data))
    return events


def _parts(message: Any) -> list:
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    if isinstance(parts, list):
        return parts
    part = message.get("part")
    return [part] if isinstance(part, dict) else []


def tool_result_from_events(events: list[tuple[str, Any]], tool_call_id: str) -> dict:
    """Pull conversation id, the tool's return content and any error out of a stream."""
    out: dict[str, Any] = {"conversation_id": None, "content": None, "returned": False,
                           "error": None, "events": len(events)}
    for name, data in events:
        if name == "conversation_id" and isinstance(data, dict):
            out["conversation_id"] = data.get("id")
        elif name == "error":
            out["error"] = data
        elif name == "message":
            for part in _parts(data):
                if (isinstance(part, dict) and part.get("part_kind") == "tool-return"
                        and part.get("tool_call_id") == tool_call_id):
                    content = part.get("content")
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except ValueError:
                            pass
                    out["content"] = content
                    out["returned"] = True
    return out


GENERATE_MESSAGE = "Looks good. Please generate this music video now with exactly those inputs."


def proposal_message(inputs: dict) -> str:
    """Chat turn that makes Flow Music's agent propose exactly these inputs."""
    start = float(inputs.get("start_s") or 0)
    parts = [
        f"Let's make a music video with the song https://flowmusic.app/song/{inputs['clip_id']} .",
        f"Style: {inputs.get('user_message') or 'match the song'}",
        f"Settings: {inputs.get('aspect_ratio') or '16:9'}, {inputs.get('resolution') or '720p'}, "
        f"{'show lyrics on screen' if inputs.get('render_lyrics') else 'no lyrics on screen'}",
    ]
    if inputs.get("duration_s"):
        parts.append(f"{int(round(float(inputs['duration_s'])))} seconds starting at {length_tag(start)}.")
    parts.append("Please propose the music video inputs for me to review — don't generate yet.")
    return " ".join(parts)


def _messages(conversation: Any) -> list:
    return (conversation or {}).get("messages") or [] if isinstance(conversation, dict) else []


def find_tool_call(conversation: Any, tool_name: str) -> Optional[dict]:
    hit = None
    for m in _messages(conversation):
        for p in m.get("parts") or []:
            if p.get("part_kind") == "tool-call" and p.get("tool_name") == tool_name:
                hit = p
    return hit


def find_tool_return(conversation: Any, tool_name: str) -> Any:
    hit = None
    for m in _messages(conversation):
        for p in m.get("parts") or []:
            if p.get("part_kind") == "tool-return" and p.get("tool_name") == tool_name:
                content = p.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except ValueError:
                        pass
                hit = content
    return hit


def proposal_mismatches(wanted: dict, proposed: dict) -> list[str]:
    """The agent rewrites the request in its own words; the numbers must survive."""
    out = []
    if proposed.get("clip_id") != wanted.get("clip_id"):
        out.append(f"clip {proposed.get('clip_id')} != {wanted.get('clip_id')}")
    for key in ("start_s", "duration_s"):
        w, p = wanted.get(key), proposed.get(key)
        if w is not None and (p is None or abs(float(p) - float(w)) > 1.0):
            out.append(f"{key} {p} != {w}")
    for key in ("aspect_ratio", "resolution", "render_lyrics"):
        if wanted.get(key) is not None and proposed.get(key) != wanted.get(key):
            out.append(f"{key} {proposed.get(key)!r} != {wanted.get(key)!r}")
    return out


class FlowMusicAPI:
    async def call(self, method: str, path: str, body: Any = None,
                   search: Optional[dict] = None, timeout_s: float = 60) -> Any:
        try:
            reply = await get_bridge().send("page_api", {
                "path": path, "method": method.upper(), "body": body, "search": search or {},
                "timeoutMs": int(timeout_s * 1000),
            }, timeout=timeout_s + 30)
        except BridgeError as e:
            raise FlowMusicError(str(e)) from e
        if reply.get("error"):
            raise FlowMusicError(str(reply["error"]))
        data = reply.get("data") or {}
        status, text = data.get("status"), data.get("text") or ""
        parsed: Any = text
        if "event-stream" not in (data.get("contentType") or ""):
            try:
                parsed = json.loads(text) if text else None
            except ValueError:
                parsed = text
        if isinstance(status, int) and status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise FlowMusicError(f"HTTP_{status} {method.upper()} {path}: {str(detail)[:400]}", status, detail)
        return parsed

    # ── reads ───────────────────────────────────────────────
    async def me(self) -> Any:
        return await self.call("GET", "/users/me")

    async def credits(self) -> Any:
        return await self.call("GET", "/billing/credits")

    async def models(self) -> Any:
        return await self.call("GET", "/models")

    async def tools(self) -> Any:
        return await self.call("GET", "/producer/tools")

    async def clips(self, clip_ids: list[str]) -> dict:
        data = await self.call("POST", "/clips", {"clip_ids": list(clip_ids)})
        return (data or {}).get("clips", {}) if isinstance(data, dict) else {}

    async def default_model(self) -> Optional[str]:
        try:
            data = await self.models()
        except FlowMusicError as e:
            logger.info("models lookup failed: %s", e)
            return None
        models = data.get("models", data) if isinstance(data, dict) else data
        if not isinstance(models, list) or not models:
            return None
        chosen = next((m for m in models if m.get("is_default")), models[0])
        return chosen.get("public_name")

    # ── generation ──────────────────────────────────────────
    async def tool_call(self, tool_name: str, args: dict, *, project_id: Optional[str] = None,
                        conversation_id: Optional[str] = None,
                        lyrics_map: Optional[dict] = None, wait_s: float = 900,
                        allow_empty: bool = False) -> dict:
        """Run one tool the way the web app does and wait for its return."""
        global _last_gen_at
        tool_call_id = str(uuid.uuid4())
        body = {
            "conversation_id": conversation_id,
            "part": {"part_kind": "tool-call", "tool_name": tool_name, "args": args,
                     "tool_call_id": tool_call_id},
            "client_context": {
                "current_song_id": None,
                "song_queue": [],
                "project_id": project_id or config.default_flow_project_id() or None,
                "selected_model": await self.default_model(),
                "lyrics_id_map": lyrics_map or {},
                "ghostwriter_version": "standard",
                "onboarding_variant": None,
                "client_platform": "web",
            },
        }
        async with _gen_lock:
            wait = _last_gen_at + config.GENERATION_MIN_INTERVAL_S - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            _last_gen_at = time.time()
            job = await self.call("POST", "/producer/tool-call", body)
        job_id = (job or {}).get("job_id") if isinstance(job, dict) else None
        if not job_id:
            raise FlowMusicError(f"tool-call returned no job_id: {str(job)[:300]}")
        logger.info("Flow Music %s → job %s", tool_name, job_id)
        text = await self.call("GET", f"/messages/{job_id}/stream", search={"last_id": "0"},
                               timeout_s=wait_s)
        result = tool_result_from_events(parse_sse(text if isinstance(text, str) else ""), tool_call_id)
        result["job_id"] = job_id
        if result["error"]:
            err = result["error"]
            kind = err.get("type") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise FlowMusicError(f"FLOW_MUSIC_{(kind or 'error').upper()}: {msg}", detail=err)
        if not result["returned"] or (result["content"] is None and not allow_empty):
            raise FlowMusicError(f"NO_TOOL_RETURN for {tool_name} (job {job_id}, {result['events']} events)")
        return result

    async def _client_context(self, project_id: Optional[str], lyrics_map: Optional[dict] = None) -> dict:
        return {
            "current_song_id": None, "song_queue": [],
            "project_id": project_id or config.default_flow_project_id() or None,
            "selected_model": await self.default_model(),
            "lyrics_id_map": lyrics_map or {}, "ghostwriter_version": "standard",
            "onboarding_variant": None, "client_platform": "web",
        }

    async def send_message(self, text: str, *, conversation_id: Optional[str] = None,
                           project_id: Optional[str] = None, wait_s: float = 600) -> dict:
        """Chat with Flow Music's own agent, like typing in the session box.
        Returns {conversation_id, job_id, events}; read the result with
        get_conversation()."""
        body = {
            "conversation_id": conversation_id,
            "parts": [{"part_kind": "user-prompt", "content": text}],
            "client_context": await self._client_context(project_id),
            "model_name": None,
            "mode": "standard",
        }
        job = await self.call("POST", "/conversation", body)
        job_id = (job or {}).get("job_id") if isinstance(job, dict) else None
        if not job_id:
            raise FlowMusicError(f"chat returned no job_id: {str(job)[:300]}")
        text_out = await self.call("GET", f"/messages/{job_id}/stream",
                                   search={"last_id": "0", "tool_progress": "true"}, timeout_s=wait_s)
        events = parse_sse(text_out if isinstance(text_out, str) else "")
        conv = next((d.get("id") for n, d in events if n == "conversation_id" and isinstance(d, dict)),
                    conversation_id)
        errors = [d for n, d in events if n == "error"]
        if errors:
            e = errors[0]
            raise FlowMusicError(f"FLOW_MUSIC_CHAT_ERROR: {e.get('message') if isinstance(e, dict) else e}", detail=e)
        return {"conversation_id": conv, "job_id": job_id, "events": events}

    async def get_conversation(self, conversation_id: str) -> Any:
        return await self.call("GET", f"/conversations/{conversation_id}")

    async def create_song(self, *, project_id: Optional[str] = None, **kwargs) -> dict:
        args, lyrics_map = build_create_song_args(**kwargs)
        result = await self.tool_call("audio__create_song", args, project_id=project_id,
                                      lyrics_map=lyrics_map)
        content = result["content"] if isinstance(result["content"], dict) else {}
        clip_ids = [c for c in (content.get("clip_id"), content.get("clip_id_b")) if c]
        if not clip_ids:
            raise FlowMusicError(f"song created but no clip id returned: {str(content)[:300]}")
        return {**result, "clip_ids": clip_ids}

    async def create_music_video(self, *, clip_id: str, user_message: str = "",
                                 aspect_ratio: str = "16:9", resolution: str = "720p",
                                 start_s: float = 0, duration_s: Optional[float] = None,
                                 render_lyrics: bool = False, style_image_url: Optional[str] = None,
                                 likeness_image_url: Optional[str] = None,
                                 project_id: Optional[str] = None,
                                 conversation_id: Optional[str] = None) -> dict:
        inputs = {
            "clip_id": clip_id, "user_message": user_message,
            "style_image_url": style_image_url, "likeness_image_url": likeness_image_url,
            "aspect_ratio": aspect_ratio, "resolution": resolution,
            "render_lyrics": render_lyrics, "start_s": start_s, "duration_s": duration_s,
        }
        # A direct client tool-call to video__create_music_video is rejected
        # ("Error while streaming parts"), with or without a proposal. What
        # works is the web app's real path: Flow Music's own agent proposes
        # (video__propose_music_video) and then runs create itself (with
        # args=null, reading the proposal). So: two chat turns, then poll the
        # render job.
        conv = conversation_id
        if not conv:
            res = await self.send_message(proposal_message(inputs), project_id=project_id, wait_s=600)
            conv = res["conversation_id"]
        proposed = find_tool_call(await self.get_conversation(conv), "video__propose_music_video")
        p_inputs = ((proposed or {}).get("args") or {}).get("inputs") or {}
        problems = proposal_mismatches(inputs, p_inputs) if proposed else ["no proposal was made"]
        if problems:
            raise FlowMusicError(f"MV_PROPOSAL_MISMATCH ({'; '.join(problems)}) — conversation {conv}",
                                 detail=p_inputs)
        await self.send_message(GENERATE_MESSAGE, conversation_id=conv, project_id=project_id, wait_s=600)
        ret = find_tool_return(await self.get_conversation(conv), "video__create_music_video")
        job_id = (ret or {}).get("job_id") if isinstance(ret, dict) else None
        if not job_id:
            raise FlowMusicError(f"MV_NOT_SUBMITTED: agent did not start the render — conversation {conv}",
                                 detail=ret)
        logger.info("Flow Music music video job %s (conversation %s)", job_id, conv)
        return {"conversation_id": conv, "job_id": job_id, "content": ret, "proposal": p_inputs}

    async def music_video_status(self, job_id: str) -> dict:
        data = await self.call("GET", f"/music-video/{job_id}/status")
        return (data or {}).get("state") or {} if isinstance(data, dict) else {}

    async def wait_for_music_video(self, job_id: str, *, timeout_s: float = 3600,
                                   every_s: float = 30) -> dict:
        deadline = time.time() + timeout_s
        state: dict = {}
        while time.time() < deadline:
            state = await self.music_video_status(job_id)
            if state.get("final_video_url"):
                return state
            if state.get("status") not in (None, "running", "queued", "pending"):
                raise FlowMusicError(
                    f"MV_{str(state.get('status')).upper()}: {state.get('error_message') or state.get('current_stage')}",
                    detail=state)
            await asyncio.sleep(every_s)
        raise FlowMusicError(f"MV_TIMEOUT after {int(timeout_s)}s (stage {state.get('current_stage')})",
                             detail=state)

    async def wait_for_clips(self, clip_ids: list[str], *, need_video: bool = False,
                             timeout_s: float = 900, every_s: float = 8) -> dict:
        """Poll clips until audio (and optionally video) is ready."""
        deadline = time.time() + timeout_s
        last: dict = {}
        while time.time() < deadline:
            last = await self.clips(clip_ids)
            ready = []
            for cid in clip_ids:
                c = last.get(cid) or {}
                done = bool(c.get("audio_url")) and (c.get("duration") or {}).get("status") == "completed"
                if need_video:
                    done = bool(c.get("video_url"))
                ready.append(done)
            if all(ready):
                return last
            await asyncio.sleep(every_s)
        raise FlowMusicError(f"CLIP_TIMEOUT after {int(timeout_s)}s", detail=last)


_api: Optional[FlowMusicAPI] = None


def get_flowmusic_api() -> FlowMusicAPI:
    global _api
    if _api is None:
        _api = FlowMusicAPI()
    return _api
