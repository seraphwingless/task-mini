"""Хранилище на Postgres (asyncpg). Тот же набор методов, что был у Sheets,
и те же строковые поля — чтобы остальной код и фронтенд не менялись."""
from __future__ import annotations

import os
import uuid
from datetime import datetime

import asyncpg

TASK_FIELDS = [
    "id", "title", "notes", "category", "priority",
    "due_at", "remind_at", "recurrence", "status",
    "created_at", "completed_at", "attachments",
    "reminded", "last_nagged_at", "reminders", "nag_on",
]

DEFAULT_CATS = [
    {"name": "личное", "emoji": "🙋‍♂️", "color": "#639922"},
    {"name": "бизнес", "emoji": "💼", "color": "#378ADD"},
    {"name": "спорт", "emoji": "⚽", "color": "#1D9E75"},
    {"name": "семья", "emoji": "👨‍👩‍👧", "color": "#7F77DD"},
]
DEFAULT_SETTINGS = {
    "default_priority": "P3", "urg_green_h": "48", "urg_yellow_h": "24", "urg_orange_h": "12",
    "nag_interval_min": "60", "lead_time_min": "60", "quiet_on": "1", "quiet_start": "23:00",
    "quiet_end": "08:00", "digest_on": "1", "digest_time": "08:00", "theme": "auto",
}

DDL = """
CREATE TABLE IF NOT EXISTS tasks(
  seq bigserial, id text PRIMARY KEY, title text DEFAULT '', notes text DEFAULT '',
  category text DEFAULT '', priority text DEFAULT 'P3', due_at text DEFAULT '',
  remind_at text DEFAULT '', recurrence text DEFAULT 'none', status text DEFAULT 'open',
  created_at text DEFAULT '', completed_at text DEFAULT '', attachments text DEFAULT '',
  reminded text DEFAULT '', last_nagged_at text DEFAULT '',
  reminders text DEFAULT '', nag_on text DEFAULT '1');
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminders text DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS nag_on text DEFAULT '1';
CREATE TABLE IF NOT EXISTS categories(
  seq bigserial, name text PRIMARY KEY, emoji text DEFAULT '', color text DEFAULT '#888780');
CREATE TABLE IF NOT EXISTS comments(
  id text PRIMARY KEY, task_id text, body text, created_at text);
CREATE TABLE IF NOT EXISTS settings(key text PRIMARY KEY, value text);
"""


class DB:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None

    async def pool(self):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, ssl=False, min_size=1, max_size=5)
            async with self._pool.acquire() as c:
                await c.execute(DDL)
                if not await c.fetchval("SELECT count(*) FROM categories"):
                    for cat in DEFAULT_CATS:
                        await c.execute("INSERT INTO categories(name,emoji,color) VALUES($1,$2,$3) "
                                        "ON CONFLICT DO NOTHING", cat["name"], cat["emoji"], cat["color"])
                if not await c.fetchval("SELECT count(*) FROM settings"):
                    for k, v in DEFAULT_SETTINGS.items():
                        await c.execute("INSERT INTO settings(key,value) VALUES($1,$2) "
                                        "ON CONFLICT DO NOTHING", k, v)
        return self._pool

    def _task(self, r) -> dict:
        return {k: (r[k] if r[k] is not None else "") for k in TASK_FIELDS}

    # ---- tasks ----
    async def list_tasks(self):
        p = await self.pool()
        rows = await p.fetch("SELECT * FROM tasks ORDER BY seq")
        return [self._task(r) for r in rows]

    async def add_task(self, task: dict):
        p = await self.pool()
        task.setdefault("id", "t" + uuid.uuid4().hex[:7])
        task.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        task.setdefault("status", "open")
        task.setdefault("nag_on", "1")
        ph = ",".join("$" + str(i + 1) for i in range(len(TASK_FIELDS)))
        await p.execute(f"INSERT INTO tasks({','.join(TASK_FIELDS)}) VALUES({ph})",
                        *[str(task.get(k, "")) for k in TASK_FIELDS])
        return {k: str(task.get(k, "")) for k in TASK_FIELDS}

    async def update_task(self, task_id: str, patch: dict):
        p = await self.pool()
        fields = [k for k in patch if k in TASK_FIELDS and k != "id"]
        if not fields:
            r = await p.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id)
            return self._task(r) if r else None
        sets = ",".join(f"{k}=${i + 2}" for i, k in enumerate(fields))
        r = await p.fetchrow(f"UPDATE tasks SET {sets} WHERE id=$1 RETURNING *",
                             task_id, *[str(patch[k]) for k in fields])
        return self._task(r) if r else None

    async def delete_task(self, task_id: str):
        p = await self.pool()
        await p.execute("DELETE FROM tasks WHERE id=$1", task_id)

    # ---- categories ----
    async def list_cats(self):
        p = await self.pool()
        rows = await p.fetch("SELECT name,emoji,color FROM categories ORDER BY seq")
        return [{"name": r["name"], "emoji": r["emoji"], "color": r["color"]} for r in rows]

    async def add_cat(self, c: dict):
        p = await self.pool()
        await p.execute("INSERT INTO categories(name,emoji,color) VALUES($1,$2,$3) "
                        "ON CONFLICT(name) DO UPDATE SET emoji=$2,color=$3",
                        c.get("name", ""), c.get("emoji", ""), c.get("color", "#888780"))
        return c

    async def update_cat(self, name: str, patch: dict):
        p = await self.pool()
        new_name = (str(patch.get("name") or name)).strip() or name
        await p.execute("UPDATE categories SET emoji=COALESCE($2,emoji), color=COALESCE($3,color), "
                        "name=$4 WHERE name=$1", name, patch.get("emoji"), patch.get("color"), new_name)
        if new_name != name:
            await p.execute("UPDATE tasks SET category=$2 WHERE category=$1", name, new_name)

    async def delete_cat(self, name: str):
        p = await self.pool()
        await p.execute("DELETE FROM categories WHERE name=$1", name)

    # ---- comments ----
    async def list_comments(self, task_id: str):
        p = await self.pool()
        rows = await p.fetch("SELECT id,task_id,body,created_at FROM comments WHERE task_id=$1 ORDER BY created_at", task_id)
        return [{"id": r["id"], "task_id": r["task_id"], "text": r["body"], "created_at": r["created_at"]} for r in rows]

    async def add_comment(self, task_id: str, text: str):
        p = await self.pool()
        c = {"id": "c" + uuid.uuid4().hex[:7], "task_id": task_id, "text": text,
             "created_at": datetime.now().isoformat(timespec="seconds")}
        await p.execute("INSERT INTO comments(id,task_id,body,created_at) VALUES($1,$2,$3,$4)",
                        c["id"], c["task_id"], c["text"], c["created_at"])
        return c

    # ---- settings ----
    async def get_settings(self):
        p = await self.pool()
        rows = await p.fetch("SELECT key,value FROM settings")
        return {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}

    async def set_settings(self, patch: dict):
        p = await self.pool()
        for k, v in patch.items():
            await p.execute("INSERT INTO settings(key,value) VALUES($1,$2) "
                            "ON CONFLICT(key) DO UPDATE SET value=$2", k, str(v))
        return await self.get_settings()
