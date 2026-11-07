"""Projects — a album / channel / batch of songs that share a style."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.db import crud
from agent.utils.paths import project_dir, slugify

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    description: Optional[str] = None
    artist_style: Optional[str] = None
    language: str = "en"
    flowkit_project_id: Optional[str] = None


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    artist_style: Optional[str] = None
    language: Optional[str] = None
    flowkit_project_id: Optional[str] = None
    status: Optional[str] = None


@router.post("")
async def create_project(body: ProjectIn):
    row = await crud.insert("project", {**body.model_dump(), "slug": slugify(body.name)})
    project_dir(row["slug"])
    return row


@router.get("")
async def list_projects(status: Optional[str] = None):
    return await crud.list_rows("project", {"status": status})


@router.get("/{pid}")
async def get_project(pid: str):
    row = await crud.get("project", pid)
    if not row:
        raise HTTPException(404, "project not found")
    songs = await crud.list_rows("song", {"project_id": pid}, order="display_order ASC, created_at ASC",
                                 columns="id, title, status, kind, audio_duration_s, audio_path")
    videos = await crud.list_rows("music_video", {"project_id": pid},
                                  columns="id, song_id, mode, status, output_path")
    return {**row, "output_dir": str(project_dir(row["slug"])), "songs": songs, "music_videos": videos}


@router.patch("/{pid}")
async def patch_project(pid: str, body: ProjectPatch):
    values = body.model_dump(exclude_unset=True)
    if "status" in values and values["status"] not in ("ACTIVE", "ARCHIVED"):
        raise HTTPException(400, "status must be ACTIVE or ARCHIVED")
    row = await crud.update("project", pid, values)
    if not row:
        raise HTTPException(404, "project not found")
    return row


@router.delete("/{pid}")
async def delete_project(pid: str):
    if not await crud.delete("project", pid):
        raise HTTPException(404, "project not found")
    return {"ok": True}
