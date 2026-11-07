"""Songs and the jobs that make them."""
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent.db import crud
from agent.services import recipes as rc
from agent.services.music import audio_file_variables
from agent.services.post_process import FFmpegError, probe_duration
from agent.utils.paths import slugify, songs_dir

router = APIRouter(prefix="/songs", tags=["songs"])


class SongIn(BaseModel):
    project_id: str
    title: str
    prompt: Optional[str] = None
    lyrics: Optional[str] = None
    instrumental: bool = False
    bpm: Optional[int] = Field(None, ge=30, le=300)
    duration_s: Optional[float] = Field(None, gt=0, le=600)
    vocal: Optional[str] = None
    language: Optional[str] = None
    display_order: int = 0


class SongPatch(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    lyrics: Optional[str] = None
    instrumental: Optional[bool] = None
    bpm: Optional[int] = Field(None, ge=30, le=300)
    duration_s: Optional[float] = Field(None, gt=0, le=600)
    vocal: Optional[str] = None
    language: Optional[str] = None
    display_order: Optional[int] = None
    selected_take: Optional[int] = None
    remote_id: Optional[str] = None


class GenerateIn(BaseModel):
    recipe: Optional[str] = None           # override the default recipe
    variables: dict[str, Any] = {}          # extra / override recipe variables


class DeriveIn(BaseModel):
    """Extend or cover an existing song into a new song row."""
    title: Optional[str] = None
    prompt: Optional[str] = None
    lyrics: Optional[str] = None
    continue_at: Optional[float] = None
    recipe: Optional[str] = None
    variables: dict[str, Any] = {}


class ImportIn(BaseModel):
    project_id: str
    title: str
    path: str                               # local audio file
    upload: bool = False                     # also upload to Flow Music (needs upload_audio recipe)


async def _song_or_404(sid: str) -> dict:
    song = await crud.get("song", sid)
    if not song:
        raise HTTPException(404, "song not found")
    return song


def _require_recipe(name: str) -> None:
    if not rc.has_recipe(name):
        raise HTTPException(
            409,
            f"recipe '{name}' not recorded yet — run /fm-capture to record it from the Flow Music UI",
        )


async def song_variables(song: dict) -> dict:
    project = await crud.get("project", song["project_id"])
    prompt = song.get("prompt") or ""
    style = (project or {}).get("artist_style")
    if style and style.lower() not in prompt.lower():
        prompt = f"{prompt}. {style}".strip(". ") if prompt else style
    return {
        "title": song["title"],
        "prompt": prompt,
        "lyrics": "" if song.get("instrumental") else (song.get("lyrics") or ""),
        "instrumental": bool(song.get("instrumental")),
        "bpm": song.get("bpm"),
        "duration_s": song.get("duration_s"),
        "vocal": song.get("vocal") or "",
        "language": song.get("language") or (project or {}).get("language") or "en",
        "remote_id": song.get("remote_id"),
    }


async def queue(kind: str, recipe: str, song: Optional[dict], variables: dict,
                project_id: Optional[str] = None) -> dict:
    _require_recipe(recipe)
    return await crud.insert("request", {
        "type": kind,
        "recipe": recipe,
        "song_id": song["id"] if song else None,
        "project_id": project_id or (song or {}).get("project_id"),
        "variables": variables,
    })


@router.post("")
async def create_song(body: SongIn):
    if not await crud.get("project", body.project_id):
        raise HTTPException(404, "project not found")
    return await crud.insert("song", {**body.model_dump(), "slug": slugify(body.title)})


@router.get("")
async def list_songs(project_id: Optional[str] = None, status: Optional[str] = None):
    return await crud.list_rows("song", {"project_id": project_id, "status": status},
                                order="display_order ASC, created_at ASC")


@router.get("/{sid}")
async def get_song(sid: str):
    song = await _song_or_404(sid)
    song["requests"] = await crud.list_rows(
        "request", {"song_id": sid}, limit=20,
        columns="id, type, status, recipe, error_message, created_at")
    return song


@router.patch("/{sid}")
async def patch_song(sid: str, body: SongPatch):
    song = await _song_or_404(sid)
    values = body.model_dump(exclude_unset=True)
    if "title" in values:
        values["slug"] = slugify(values["title"])
    if "selected_take" in values:
        takes = song.get("takes") or []
        i = values["selected_take"]
        if not 0 <= i < len(takes):
            raise HTTPException(400, f"selected_take must be 0..{len(takes) - 1}")
        values["audio_path"] = takes[i]["path"]
        values["audio_duration_s"] = takes[i].get("duration_s")
    return await crud.update("song", sid, values)


@router.delete("/{sid}")
async def delete_song(sid: str):
    if not await crud.delete("song", sid):
        raise HTTPException(404, "song not found")
    return {"ok": True}


@router.post("/{sid}/generate")
async def generate_song(sid: str, body: GenerateIn = GenerateIn()):
    song = await _song_or_404(sid)
    if not song.get("prompt") and not song.get("lyrics"):
        raise HTTPException(400, "song needs a prompt or lyrics first")
    variables = {**await song_variables(song), **body.variables}
    req = await queue("GENERATE_SONG", body.recipe or "generate_song", song, variables)
    await crud.update("song", sid, {"status": "PROCESSING", "error_message": None})
    return req


@router.post("/{sid}/lyrics")
async def generate_lyrics(sid: str, body: GenerateIn = GenerateIn()):
    song = await _song_or_404(sid)
    variables = {**await song_variables(song), **body.variables}
    if not variables.get("prompt"):
        raise HTTPException(400, "song needs a prompt to write lyrics from")
    return await queue("GENERATE_LYRICS", body.recipe or "generate_lyrics", song, variables)


async def _derive(sid: str, body: DeriveIn, kind: str, recipe_default: str, song_kind: str):
    parent = await _song_or_404(sid)
    if not parent.get("remote_id"):
        raise HTTPException(400, "source song has no remote_id — generate or upload it first")
    title = body.title or f"{parent['title']} ({song_kind.lower()})"
    child = await crud.insert("song", {
        "project_id": parent["project_id"], "title": title, "slug": slugify(title),
        "kind": song_kind, "parent_song_id": parent["id"],
        "prompt": body.prompt or parent.get("prompt"),
        "lyrics": body.lyrics if body.lyrics is not None else parent.get("lyrics"),
        "instrumental": parent.get("instrumental", False), "bpm": parent.get("bpm"),
        "vocal": parent.get("vocal"), "language": parent.get("language"),
        "status": "PROCESSING",
    })
    variables = {**await song_variables(child), "remote_id": parent["remote_id"],
                 "continue_at": body.continue_at, **body.variables}
    req = await queue(kind, body.recipe or recipe_default, child, variables)
    return {"song": child, "request": req}


@router.post("/{sid}/extend")
async def extend_song(sid: str, body: DeriveIn = DeriveIn()):
    return await _derive(sid, body, "EXTEND_SONG", "extend_song", "EXTEND")


@router.post("/{sid}/cover")
async def cover_song(sid: str, body: DeriveIn = DeriveIn()):
    if not body.prompt:
        raise HTTPException(400, "cover needs a prompt describing the new style")
    return await _derive(sid, body, "COVER_SONG", "cover_song", "COVER")


@router.post("/import")
async def import_song(body: ImportIn):
    project = await crud.get("project", body.project_id)
    if not project:
        raise HTTPException(404, "project not found")
    src = Path(body.path).expanduser()
    if not src.is_file():
        raise HTTPException(400, f"file not found: {src}")
    if body.upload:
        _require_recipe("upload_audio")
    slug = slugify(body.title)
    dest = songs_dir(project["slug"]) / f"{slug}_import{src.suffix.lower()}"
    shutil.copyfile(src, dest)
    try:
        duration = round(await probe_duration(dest), 2)
    except FFmpegError:
        duration = None
    take = {"url": None, "path": str(dest), "duration_s": duration}
    song = await crud.insert("song", {
        "project_id": project["id"], "title": body.title, "slug": slug, "kind": "UPLOAD",
        "status": "COMPLETED", "takes": [take], "audio_path": str(dest), "audio_duration_s": duration,
    })
    req = None
    if body.upload:
        req = await queue("UPLOAD_AUDIO", "upload_audio", song, audio_file_variables(dest))
    return {"song": song, "request": req}


@router.get("/{sid}/audio")
async def song_audio(sid: str, take: Optional[int] = None):
    song = await _song_or_404(sid)
    takes = song.get("takes") or []
    path = takes[take]["path"] if take is not None and 0 <= take < len(takes) else song.get("audio_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no audio file for this song yet")
    return FileResponse(path)
