"""Small generic CRUD layer over the SQLite tables.

Rows come back as plain dicts. JSON columns (song.takes, request.variables,
request.result, music_video.plan) are decoded on read and encoded on write so
callers never handle raw JSON strings.
"""
import json
import uuid
from typing import Any, Optional

from agent.db.schema import get_db

JSON_COLUMNS = {
    "song": {"takes"},
    "request": {"variables", "result"},
    "music_video": {"plan"},
}
BOOL_COLUMNS = {"song": {"instrumental"}}
TABLES = {"project", "song", "request", "music_video", "capture"}


def new_id() -> str:
    return str(uuid.uuid4())


def _decode(table: str, row) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for col in JSON_COLUMNS.get(table, ()):
        if out.get(col) is not None:
            try:
                out[col] = json.loads(out[col])
            except (TypeError, ValueError):
                pass
    for col in BOOL_COLUMNS.get(table, ()):
        if col in out and out[col] is not None:
            out[col] = bool(out[col])
    return out


def _encode(table: str, values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key in JSON_COLUMNS.get(table, ()) and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if key in BOOL_COLUMNS.get(table, ()) and isinstance(value, bool):
            value = int(value)
        out[key] = value
    return out


def _check(table: str) -> None:
    if table not in TABLES:
        raise ValueError(f"unknown table {table}")


async def insert(table: str, values: dict) -> dict:
    _check(table)
    values = _encode(table, {"id": new_id(), **values})
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    db = await get_db()
    await db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    await db.commit()
    return await get(table, values["id"])


async def get(table: str, row_id: str) -> Optional[dict]:
    _check(table)
    db = await get_db()
    async with db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)) as cur:
        return _decode(table, await cur.fetchone())


async def update(table: str, row_id: str, values: dict) -> Optional[dict]:
    _check(table)
    values = {k: v for k, v in values.items() if k != "id"}
    if not values:
        return await get(table, row_id)
    values = _encode(table, values)
    if table != "capture":
        values["updated_at"] = _now_sql()
    sets = ", ".join(f"{k} = ?" for k in values)
    db = await get_db()
    await db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))
    await db.commit()
    return await get(table, row_id)


async def delete(table: str, row_id: str) -> bool:
    _check(table)
    db = await get_db()
    cur = await db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    await db.commit()
    return cur.rowcount > 0


async def delete_all(table: str) -> int:
    _check(table)
    db = await get_db()
    cur = await db.execute(f"DELETE FROM {table}")
    await db.commit()
    return cur.rowcount


async def list_rows(
    table: str,
    where: Optional[dict[str, Any]] = None,
    order: str = "created_at DESC",
    limit: int = 200,
    columns: str = "*",
) -> list[dict]:
    _check(table)
    where = {k: v for k, v in (where or {}).items() if v is not None}
    clauses, params = [], []
    for key, value in where.items():
        if isinstance(value, (list, tuple, set)):
            clauses.append(f"{key} IN ({', '.join('?' for _ in value)})")
            params.extend(value)
        else:
            clauses.append(f"{key} = ?")
            params.append(value)
    sql = f"SELECT {columns} FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(int(limit))
    db = await get_db()
    async with db.execute(sql, params) as cur:
        return [_decode(table, r) for r in await cur.fetchall()]


def _now_sql() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
