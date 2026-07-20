"""Хранилище на Postgres (asyncpg), многопользовательское.
Все данные привязаны к user_id (Telegram id владельца записи).
allowed_users — список тех, кому разрешён доступ (инвайт-онли)."""
from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg

TASK_FIELDS = [
    "id", "title", "notes", "category", "priority",
    "due_at", "remind_at", "recurrence", "status",
    "created_at", "completed_at", "attachments",
    "reminded", "last_nagged_at", "reminders", "nag_on",
    "checklist", "checked_date",
]

DEFAULT_CATS = [
    {"name": "Личное", "emoji": "🙋‍♂️", "color": "#639922"},
    {"name": "Бизнес", "emoji": "💼", "color": "#378ADD"},
    {"name": "Спорт", "emoji": "⚽", "color": "#1D9E75"},
    {"name": "Семья", "emoji": "👨‍👩‍👧", "color": "#7F77DD"},
]
DEFAULT_SETTINGS = {
    "default_priority": "P3", "urg_green_h": "48", "urg_yellow_h": "24", "urg_orange_h": "12",
    "quiet_on": "1", "quiet_start": "23:00", "quiet_end": "08:00",
    "digest_on": "1", "digest_time": "08:00",
    "evening_on": "1", "evening_time": "20:00", "theme": "auto",
}


def cap(name: str) -> str:
    """Категории всегда с большой буквы (остальной регистр не трогаем)."""
    n = (name or "").strip()
    return n[:1].upper() + n[1:] if n else n


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
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id bigint;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checklist text DEFAULT '0';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checked_date text DEFAULT '';
CREATE INDEX IF NOT EXISTS tasks_user_status_idx ON tasks(user_id, status);
CREATE TABLE IF NOT EXISTS comments(id text PRIMARY KEY, task_id text, body text, created_at text);
ALTER TABLE comments ADD COLUMN IF NOT EXISTS user_id bigint;
CREATE TABLE IF NOT EXISTS user_categories(
  seq bigserial, user_id bigint, name text, emoji text DEFAULT '', color text DEFAULT '#888780',
  PRIMARY KEY(user_id, name));
CREATE TABLE IF NOT EXISTS user_settings(user_id bigint, key text, value text, PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS allowed_users(user_id bigint PRIMARY KEY, name text DEFAULT '', added_at text DEFAULT '');
DO $$ BEGIN
  BEGIN
    UPDATE user_categories SET name = initcap(name) WHERE name <> initcap(name);
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  UPDATE tasks SET category = initcap(category)
   WHERE category IS NOT NULL AND category <> '' AND category <> initcap(category);
END $$;
"""


class DB:
    def __init__(self, dsn: str, owner_id: int):
        self._dsn = dsn
        self._owner = int(owner_id)
        self._pool = None

    async def pool(self):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, ssl=False, min_size=2, max_size=5)
            async with self._pool.acquire() as c:
                await c.execute(DDL)
                await c.execute("UPDATE tasks SET user_id=$1 WHERE user_id IS NULL", self._owner)
                await c.execute("UPDATE comments SET user_id=$1 WHERE user_id IS NULL", self._owner)
                await c.execute("INSERT INTO allowed_users(user_id,name,added_at) VALUES($1,'Владелец',$2) "
                                "ON CONFLICT DO NOTHING", self._owner, datetime.now().isoformat(timespec="seconds"))
        return self._pool

    def _task(self, r) -> dict:
        return {k: (r[k] if r[k] is not None else "") for k in TASK_FIELDS}

    # ---- access ----
    async def is_allowed(self, uid: int) -> bool:
        if int(uid) == self._owner:
            return True
        p = await self.pool()
        return bool(await p.fetchval("SELECT 1 FROM allowed_users WHERE user_id=$1", int(uid)))

    async def list_access(self):
        p = await self.pool()
        rows = await p.fetch("SELECT user_id,name,added_at FROM allowed_users ORDER BY added_at")
        return [{"user_id": str(r["user_id"]), "name": r["name"], "owner": r["user_id"] == self._owner} for r in rows]

    async def add_access(self, uid: int, name: str):
        p = await self.pool()
        await p.execute("INSERT INTO allowed_users(user_id,name,added_at) VALUES($1,$2,$3) "
                        "ON CONFLICT(user_id) DO UPDATE SET name=$2", int(uid), name,
                        datetime.now().isoformat(timespec="seconds"))

    async def remove_access(self, uid: int):
        if int(uid) == self._owner:
            return
        p = await self.pool()
        await p.execute("DELETE FROM allowed_users WHERE user_id=$1", int(uid))

    # ---- tasks ----
    async def list_tasks(self, uid: int):
        p = await self.pool()
        rows = await p.fetch("SELECT * FROM tasks WHERE user_id=$1 AND status<>'done' ORDER BY seq", int(uid))
        return [self._task(r) for r in rows]

    async def list_archive(self, uid: int):
        p = await self.pool()
        rows = await p.fetch("SELECT * FROM tasks WHERE user_id=$1 AND status='done' "
                             "ORDER BY completed_at DESC NULLS LAST, seq DESC", int(uid))
        return [self._task(r) for r in rows]

    async def add_task(self, uid: int, task: dict):
        p = await self.pool()
        task.setdefault("id", "t" + uuid.uuid4().hex[:7])
        task.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        task.setdefault("status", "open")
        task.setdefault("nag_on", "1")
        task["category"] = cap(task.get("category", ""))
        cols = TASK_FIELDS + ["user_id"]
        vals = [str(task.get(k, "")) for k in TASK_FIELDS] + [int(uid)]
        ph = ",".join("$" + str(i + 1) for i in range(len(cols)))
        await p.execute(f"INSERT INTO tasks({','.join(cols)}) VALUES({ph})", *vals)
        return {k: str(task.get(k, "")) for k in TASK_FIELDS}

    async def update_task(self, uid: int, task_id: str, patch: dict):
        p = await self.pool()
        if "category" in patch:
            patch["category"] = cap(patch["category"])
        fields = [k for k in patch if k in TASK_FIELDS and k != "id"]
        if not fields:
            r = await p.fetchrow("SELECT * FROM tasks WHERE id=$1 AND user_id=$2", task_id, int(uid))
            return self._task(r) if r else None
        sets = ",".join(f"{k}=${i + 3}" for i, k in enumerate(fields))
        r = await p.fetchrow(f"UPDATE tasks SET {sets} WHERE id=$1 AND user_id=$2 RETURNING *",
                             task_id, int(uid), *[str(patch[k]) for k in fields])
        return self._task(r) if r else None

    async def delete_task(self, uid: int, task_id: str):
        p = await self.pool()
        await p.execute("DELETE FROM tasks WHERE id=$1 AND user_id=$2", task_id, int(uid))

    # ---- categories ----
    async def list_cats(self, uid: int):
        p = await self.pool()
        rows = await p.fetch("SELECT name,emoji,color FROM user_categories WHERE user_id=$1 ORDER BY seq", int(uid))
        if not rows:
            for c in DEFAULT_CATS:
                await p.execute("INSERT INTO user_categories(user_id,name,emoji,color) VALUES($1,$2,$3,$4) "
                                "ON CONFLICT DO NOTHING", int(uid), c["name"], c["emoji"], c["color"])
            return [dict(c) for c in DEFAULT_CATS]
        return [{"name": r["name"], "emoji": r["emoji"], "color": r["color"]} for r in rows]

    async def add_cat(self, uid: int, c: dict):
        p = await self.pool()
        c["name"] = cap(c.get("name", ""))
        await p.execute("INSERT INTO user_categories(user_id,name,emoji,color) VALUES($1,$2,$3,$4) "
                        "ON CONFLICT(user_id,name) DO UPDATE SET emoji=$3,color=$4",
                        int(uid), c["name"], c.get("emoji", ""), c.get("color", "#888780"))
        return c

    async def update_cat(self, uid: int, name: str, patch: dict):
        p = await self.pool()
        new_name = cap(str(patch.get("name") or name)) or name
        await p.execute("UPDATE user_categories SET emoji=COALESCE($3,emoji), color=COALESCE($4,color), "
                        "name=$5 WHERE user_id=$1 AND name=$2",
                        int(uid), name, patch.get("emoji"), patch.get("color"), new_name)
        if new_name != name:
            await p.execute("UPDATE tasks SET category=$3 WHERE user_id=$1 AND category=$2", int(uid), name, new_name)

    async def delete_cat(self, uid: int, name: str):
        p = await self.pool()
        await p.execute("DELETE FROM user_categories WHERE user_id=$1 AND name=$2", int(uid), name)

    # ---- comments ----
    async def list_comments(self, uid: int, task_id: str):
        p = await self.pool()
        rows = await p.fetch("SELECT id,task_id,body,created_at FROM comments WHERE task_id=$1 AND user_id=$2 "
                             "ORDER BY created_at", task_id, int(uid))
        return [{"id": r["id"], "task_id": r["task_id"], "text": r["body"], "created_at": r["created_at"]} for r in rows]

    async def add_comment(self, uid: int, task_id: str, text: str):
        p = await self.pool()
        c = {"id": "c" + uuid.uuid4().hex[:7], "task_id": task_id, "text": text,
             "created_at": datetime.now().isoformat(timespec="seconds")}
        await p.execute("INSERT INTO comments(id,task_id,body,created_at,user_id) VALUES($1,$2,$3,$4,$5)",
                        c["id"], c["task_id"], c["text"], c["created_at"], int(uid))
        return c

    # ---- settings ----
    async def get_settings(self, uid: int):
        p = await self.pool()
        rows = await p.fetch("SELECT key,value FROM user_settings WHERE user_id=$1", int(uid))
        if not rows:
            for k, v in DEFAULT_SETTINGS.items():
                await p.execute("INSERT INTO user_settings(user_id,key,value) VALUES($1,$2,$3) "
                                "ON CONFLICT DO NOTHING", int(uid), k, v)
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}

    async def set_settings(self, uid: int, patch: dict):
        p = await self.pool()
        for k, v in patch.items():
            await p.execute("INSERT INTO user_settings(user_id,key,value) VALUES($1,$2,$3) "
                            "ON CONFLICT(user_id,key) DO UPDATE SET value=$3", int(uid), k, str(v))
        return await self.get_settings(uid)
