"""Mini App backend: owner-only API поверх Google Sheets + отдача фронтенда.

Каждый запрос к /api/* проверяет подпись Telegram initData и что это владелец.
Фронтенд лежит в static/index.html и открывается как Telegram Mini App.
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import AuthError, verify_init_data
from sheets import Sheets

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SHEET_ID = os.environ["SHEET_ID"]

store = Sheets(SHEET_ID)
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


class TaskPatch(BaseModel):
    title: str | None = None
    category: str | None = None
    priority: str | None = None
    due_at: str | None = None
    recurrence: str | None = None
    notes: str | None = None
    status: str | None = None


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/tasks")
async def list_tasks(user=Depends(require_owner)):
    tasks = await store.list()
    return [t for t in tasks if t.get("status") != "done"]


@app.post("/api/tasks")
async def create_task(body: TaskIn, user=Depends(require_owner)):
    task = body.model_dump()
    if task.get("due_at"):
        task["remind_at"] = task["due_at"]
    return await store.add(task)


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: TaskPatch, user=Depends(require_owner)):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "due_at" in patch:
        patch["remind_at"] = patch["due_at"]
        patch["reminded"] = ""
        patch["last_nagged_at"] = ""
    updated = await store.update(task_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.post("/api/tasks/{task_id}/done")
async def complete_task(task_id: str, user=Depends(require_owner)):
    updated = await store.update(task_id, {
        "status": "done",
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    })
    if updated is None:
        raise HTTPException(status_code=404, detail="not found")
    return updated


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, user=Depends(require_owner)):
    await store.delete(task_id)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))
