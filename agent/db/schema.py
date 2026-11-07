"""SQLite schema — async via aiosqlite. One shared connection per process."""
import asyncio
import logging
from typing import Optional

import aiosqlite

from agent import config

logger = logging.getLogger(__name__)

_db: Optional[aiosqlite.Connection] = None
_lock = asyncio.Lock()

_NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS project (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL,
    description   TEXT,
    artist_style  TEXT,                 -- default style prompt for every song
    language      TEXT NOT NULL DEFAULT 'en',
    flow_music_project_id TEXT,         -- project on flowmusic.app ({{flow_project_id}})
    flowkit_project_id TEXT,            -- linked Flow Kit project (music videos)
    status        TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
    created_at    TEXT NOT NULL DEFAULT {_NOW},
    updated_at    TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE IF NOT EXISTS song (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    slug           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'ORIGINAL'
                   CHECK(kind IN ('ORIGINAL','EXTEND','COVER','UPLOAD')),
    parent_song_id TEXT REFERENCES song(id) ON DELETE SET NULL,
    prompt         TEXT,                -- style / description prompt
    lyrics         TEXT,                -- [Verse] / [Chorus] tagged lyrics
    instrumental   INTEGER NOT NULL DEFAULT 0,
    bpm            INTEGER,
    duration_s     REAL,                -- requested length
    vocal          TEXT,                -- vocal description, e.g. "warm female alto"
    language       TEXT,
    display_order  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'DRAFT'
                   CHECK(status IN ('DRAFT','PROCESSING','COMPLETED','FAILED')),
    remote_id      TEXT,                -- Flow Music's id for the track
    takes          TEXT NOT NULL DEFAULT '[]',  -- JSON [{{url, path, remote_id, duration_s}}]
    selected_take  INTEGER NOT NULL DEFAULT 0,
    audio_path     TEXT,                -- local file of the selected take
    cover_path     TEXT,
    audio_duration_s REAL,              -- measured with ffprobe
    error_message  TEXT,
    created_at     TEXT NOT NULL DEFAULT {_NOW},
    updated_at     TEXT NOT NULL DEFAULT {_NOW}
);
CREATE INDEX IF NOT EXISTS idx_song_project ON song(project_id);

CREATE TABLE IF NOT EXISTS request (
    id             TEXT PRIMARY KEY,
    project_id     TEXT REFERENCES project(id) ON DELETE CASCADE,
    song_id        TEXT REFERENCES song(id) ON DELETE CASCADE,
    type           TEXT NOT NULL CHECK(type IN (
                       'GENERATE_SONG','EXTEND_SONG','COVER_SONG',
                       'GENERATE_LYRICS','UPLOAD_AUDIO','GENERATE_MUSIC_VIDEO',
                       'RUN_RECIPE')),
    recipe         TEXT NOT NULL,       -- recipe used for the submit
    variables      TEXT NOT NULL DEFAULT '{{}}',  -- JSON
    status         TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK(status IN ('PENDING','PROCESSING','POLLING','COMPLETED','FAILED')),
    job_id         TEXT,                -- remote job / operation id
    result         TEXT,                -- JSON: extracted values
    error_message  TEXT,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    poll_count     INTEGER NOT NULL DEFAULT 0,
    submitted_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT {_NOW},
    updated_at     TEXT NOT NULL DEFAULT {_NOW}
);
CREATE INDEX IF NOT EXISTS idx_request_status ON request(status);
CREATE INDEX IF NOT EXISTS idx_request_song ON request(song_id);

CREATE TABLE IF NOT EXISTS music_video (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    song_id       TEXT NOT NULL REFERENCES song(id) ON DELETE CASCADE,
    mode          TEXT NOT NULL DEFAULT 'FLOWKIT'
                  CHECK(mode IN ('VISUALIZER','FLOWKIT','NATIVE')),
    orientation   TEXT NOT NULL DEFAULT 'HORIZONTAL'
                  CHECK(orientation IN ('HORIZONTAL','VERTICAL')),
    status        TEXT NOT NULL DEFAULT 'DRAFT'
                  CHECK(status IN ('DRAFT','PROCESSING','COMPLETED','FAILED')),
    plan          TEXT,                 -- JSON scene plan
    flowkit_video_id TEXT,
    source_video_path TEXT,             -- visuals before muxing the song
    output_path   TEXT,
    error_message TEXT,
    created_at    TEXT NOT NULL DEFAULT {_NOW},
    updated_at    TEXT NOT NULL DEFAULT {_NOW}
);

CREATE TABLE IF NOT EXISTS capture (
    id             TEXT PRIMARY KEY,
    page_url       TEXT,
    url            TEXT NOT NULL,
    method         TEXT NOT NULL,
    status_code    INTEGER,
    request_content_type TEXT,
    request_body   TEXT,
    response_content_type TEXT,
    response_text  TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT {_NOW}
);
"""

# Columns added after the first release: (table, column, declaration).
MIGRATIONS = [
    ("project", "flow_music_project_id", "TEXT"),
]


async def get_db() -> aiosqlite.Connection:
    global _db
    async with _lock:
        if _db is None:
            config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _db = await aiosqlite.connect(str(config.DB_PATH))
            _db.row_factory = aiosqlite.Row
            await _db.execute("PRAGMA journal_mode=WAL")
            await _db.execute("PRAGMA foreign_keys=ON")
        return _db


async def init_db() -> None:
    db = await get_db()
    await db.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    await db.commit()
    logger.info("Database ready at %s", config.DB_PATH)


async def close_db() -> None:
    global _db
    async with _lock:
        if _db is not None:
            await _db.close()
            _db = None
