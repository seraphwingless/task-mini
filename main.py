"""Mini App backend: многопользовательский, инвайт-онли.
Подпись Telegram проверяется у всех; пускаются только разрешённые (allowed_users);
каждый видит только свои данные. Управление доступом — только у владельца."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import AuthError, verify_user
from db import DB

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

store = DB(os.environ["DATABASE_URL"], OWNER_ID)
app = FastAPI(title="Spender Tasks Mini App")
HERE = os.path.dirname(__file__)


@app.on_event("startup")
async def warmup():
    """Пул и DDL прогреваем на старте, чтобы первый заход пользователя не ждал."""
    try:
        await store.pool()
    except Exception:  # noqa: BLE001
        pass


async def current_user(x_init: str | None = Header(None, alias="X-Telegram-Init-Data")) -> int:
    try:
        u = verify_user(x_init or "", BOT_TOKEN)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    uid = int(u.get("id", 0))
    if not await store.is_allowed(uid):
        raise HTTPException(status_code=403, detail="not allowed")
    return uid


async def owner_only(uid: int = Depends(current_user)) -> int:
    if uid != OWNER_ID:
        raise HTTPException(status_code=403, detail="owner only")
    return uid


class TaskIn(BaseModel):
    title: str
    category: str = ""
    priority: str = "P3"
    due_at: str = ""
    recurrence: str = "none"
    notes: str = ""
    reminders: str = ""
    nag_on: str = "1"
    checklist: str = "0"


class TaskPatch(BaseModel):
    title: str | None = None
    category: str | None = None
    priority: str | None = None
    due_at: str | None = None
    recurrence: str | None = None
    notes: str | None = None
    status: str | None = None
    reminders: str | None = None
    nag_on: str | None = None
    checklist: str | None = None
    checked_date: str | None = None


class CatIn(BaseModel):
    name: str
    emoji: str = ""
    color: str = "#888780"


class CatPatch(BaseModel):
    name: str | None = None
    emoji: str | None = None
    color: str | None = None


class CommentIn(BaseModel):
    text: str


class AccessIn(BaseModel):
    user_id: str
    name: str = ""


@app.get("/api/me")
async def me(uid: int = Depends(current_user)):
    return {"user_id": str(uid), "is_owner": uid == OWNER_ID}


@app.get("/api/bootstrap")
async def bootstrap(uid: int = Depends(current_user)):
    """Всё для первого экрана одним запросом — вместо четырёх."""
    return {
        "me": {"user_id": str(uid), "is_owner": uid == OWNER_ID},
        "settings": await store.get_settings(uid),
        "cats": await store.list_cats(uid),
        "tasks": await store.list_tasks(uid),
    }


# ---- tasks ----
@app.get("/api/tasks")
async def list_tasks(uid: int = Depends(current_user)):
    return await store.list_tasks(uid)


@app.get("/api/archive")
async def list_archive(uid: int = Depends(current_user)):
    return await store.list_archive(uid)


@app.post("/api/tasks")
async def create_task(body: TaskIn, uid: int = Depends(current_user)):
    task = body.model_dump()
    if task.get("due_at"):
        task["remind_at"] = task["due_at"]
    return await store.add_task(uid, task)


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: TaskPatch, uid: int = Depends(current_user)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "due_at" in patch:
        patch["remind_at"] = patch["due_at"]
        patch["reminded"] = ""
        patch["last_nagged_at"] = ""
    updated = await store.update_task(uid, task_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.post("/api/tasks/{task_id}/done")
async def complete_task(task_id: str, uid: int = Depends(current_user)):
    updated = await store.update_task(uid, task_id, {
        "status": "done", "completed_at": datetime.now().isoformat(timespec="seconds")})
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, uid: int = Depends(current_user)):
    await store.delete_task(uid, task_id)
    return {"ok": True}


# ---- categories ----
@app.get("/api/categories")
async def list_cats(uid: int = Depends(current_user)):
    return await store.list_cats(uid)


@app.post("/api/categories")
async def add_cat(body: CatIn, uid: int = Depends(current_user)):
    return await store.add_cat(uid, body.model_dump())


@app.patch("/api/categories/{name}")
async def update_cat(name: str, body: CatPatch, uid: int = Depends(current_user)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    await store.update_cat(uid, name, patch)
    return {"ok": True}


@app.delete("/api/categories/{name}")
async def delete_cat(name: str, uid: int = Depends(current_user)):
    await store.delete_cat(uid, name)
    return {"ok": True}


# ---- comments ----
@app.get("/api/tasks/{task_id}/comments")
async def list_comments(task_id: str, uid: int = Depends(current_user)):
    return await store.list_comments(uid, task_id)


@app.post("/api/tasks/{task_id}/comments")
async def add_comment(task_id: str, body: CommentIn, uid: int = Depends(current_user)):
    return await store.add_comment(uid, task_id, body.text)


# ---- settings ----
@app.get("/api/settings")
async def get_settings(uid: int = Depends(current_user)):
    return await store.get_settings(uid)


@app.put("/api/settings")
async def put_settings(body: dict, uid: int = Depends(current_user)):
    return await store.set_settings(uid, body)


# ---- access (owner only) ----
@app.get("/api/access")
async def list_access(uid: int = Depends(owner_only)):
    return await store.list_access()


async def tg_notify(chat_id: int, text: str) -> bool:
    """Пишем пользователю от имени бота. False — значит бот не может ему написать
    (обычно человек ещё не нажал Start)."""
    def _post() -> bool:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    try:
        return await asyncio.to_thread(_post)
    except Exception:  # noqa: BLE001
        return False


@app.post("/api/access")
async def add_access(body: AccessIn, uid: int = Depends(owner_only)):
    try:
        target = int(body.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad id")
    await store.add_access(target, body.name)
    notified = await tg_notify(target, "✅ Тебе открыли доступ к задачам.\n\n"
                                       "Открой приложение через меню бота — задачи, чеклист "
                                       "и напоминания у тебя свои, отдельные.")
    return {"ok": True, "notified": notified}


@app.delete("/api/access/{target}")
async def remove_access(target: int, uid: int = Depends(owner_only)):
    await store.remove_access(target)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"),
                        headers={"Cache-Control": "no-cache"})
