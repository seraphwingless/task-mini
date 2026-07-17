"""Mini App backend: owner-only API поверх Google Sheets + отдача фронтенда.
Каждый запрос к /api/* проверяет подпись Telegram initData и что это владелец."""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import AuthError, verify_init_data
from db import DB

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

store = DB(os.environ["DATABASE_URL"])
app = FastAPI(title="Spender Tasks Mini App")
HERE = os.path.dirname(__file__)


async def require_owner(x_init: str | None = Header(None, alias="X-Telegram-Init-Data")):
    try:
        return verify_init_data(x_init or "", BOT_TOKEN, OWNER_ID)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


class TaskIn(BaseModel):
    title: str
    category: str = ""
    priority: str = "P3"
    due_at: str = ""
    recurrence: str = "none"
    notes: str = ""
    reminders: str = ""
    nag_on: str = "1"


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


@app.get("/api/health")
async def health():
    return {"ok": True}


# ---- tasks ----
@app.get("/api/tasks")
async def list_tasks(user=Depends(require_owner)):
    return [t for t in await store.list_tasks() if t.get("status") != "done"]


@app.post("/api/tasks")
async def create_task(body: TaskIn, user=Depends(require_owner)):
    task = body.model_dump()
    if task.get("due_at"):
        task["remind_at"] = task["due_at"]
    return await store.add_task(task)


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: TaskPatch, user=Depends(require_owner)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "due_at" in patch:
        patch["remind_at"] = patch["due_at"]
        patch["reminded"] = ""
        patch["last_nagged_at"] = ""
    updated = await store.update_task(task_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.post("/api/tasks/{task_id}/done")
async def complete_task(task_id: str, user=Depends(require_owner)):
    updated = await store.update_task(task_id, {
        "status": "done",
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    })
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, user=Depends(require_owner)):
    await store.delete_task(task_id)
    return {"ok": True}


# ---- categories ----
@app.get("/api/categories")
async def list_cats(user=Depends(require_owner)):
    return await store.list_cats()


@app.post("/api/categories")
async def add_cat(body: CatIn, user=Depends(require_owner)):
    return await store.add_cat(body.model_dump())


@app.patch("/api/categories/{name}")
async def update_cat(name: str, body: CatPatch, user=Depends(require_owner)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    await store.update_cat(name, patch)
    return {"ok": True}


@app.delete("/api/categories/{name}")
async def delete_cat(name: str, user=Depends(require_owner)):
    await store.delete_cat(name)
    return {"ok": True}


# ---- comments ----
@app.get("/api/tasks/{task_id}/comments")
async def list_comments(task_id: str, user=Depends(require_owner)):
    return await store.list_comments(task_id)


@app.post("/api/tasks/{task_id}/comments")
async def add_comment(task_id: str, body: CommentIn, user=Depends(require_owner)):
    return await store.add_comment(task_id, body.text)


# ---- settings ----
@app.get("/api/settings")
async def get_settings(user=Depends(require_owner)):
    return await store.get_settings()


@app.put("/api/settings")
async def put_settings(body: dict, user=Depends(require_owner)):
    return await store.set_settings(body)


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))
