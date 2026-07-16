"""Хранилище в Google Sheets.
Листы:
  Tasks      — задачи (те же колонки, что у бота, чтобы не конфликтовать).
  Categories — категории пользователя (name, emoji, color).
  Comments   — комментарии к задачам (id, task_id, text, created_at).
gspread синхронный, оборачиваем в to_thread."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TASK_HEADER = [
    "id", "title", "notes", "category", "priority",
    "due_at", "remind_at", "recurrence", "status",
    "created_at", "completed_at", "attachments",
    "reminded", "last_nagged_at",
]
CAT_HEADER = ["name", "emoji", "color"]
COMMENT_HEADER = ["id", "task_id", "text", "created_at"]
SETTINGS_HEADER = ["key", "value"]

DEFAULT_SETTINGS = {
    "default_priority": "P3",
    "urg_green_h": "48",
    "urg_yellow_h": "24",
    "urg_orange_h": "12",
    "nag_interval_min": "60",
    "lead_time_min": "60",
    "quiet_on": "1",
    "quiet_start": "23:00",
    "quiet_end": "08:00",
    "digest_on": "1",
    "digest_time": "08:00",
    "theme": "auto",
}

DEFAULT_CATS = [
    {"name": "личное", "emoji": "🙋‍♂️", "color": "#639922"},
    {"name": "бизнес", "emoji": "💼", "color": "#378ADD"},
    {"name": "спорт", "emoji": "⚽", "color": "#1D9E75"},
    {"name": "семья", "emoji": "👨‍👩‍👧", "color": "#7F77DD"},
]


def _creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    path = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


class Sheets:
    def __init__(self, sheet_id: str):
        self._sheet_id = sheet_id
        self._sh = None
        self._cache: dict[str, gspread.Worksheet] = {}

    def _book(self):
        if self._sh is None:
            self._sh = gspread.authorize(_creds()).open_by_key(self._sheet_id)
        return self._sh

    def _ws(self, name: str, header: list[str]) -> gspread.Worksheet:
        if name in self._cache:
            return self._cache[name]
        sh = self._book()
        try:
            ws = sh.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=1000, cols=max(len(header), 4))
        if ws.row_values(1) != header:
            ws.update([header], "A1")
        self._cache[name] = ws
        return ws

    # ---------- tasks ----------
    def _tasks(self):
        return self._ws("Tasks", TASK_HEADER)

    def _list_tasks(self) -> list[dict]:
        ws = self._tasks()
        rows = ws.get_all_records(expected_headers=TASK_HEADER, numericise_ignore=["all"])
        return [{k: str(r.get(k, "")) for k in TASK_HEADER} for r in rows]

    def _task_row(self, task_id: str) -> Optional[int]:
        for i, v in enumerate(self._tasks().col_values(1), start=1):
            if v == task_id:
                return i
        return None

    def _add_task(self, task: dict) -> dict:
        ws = self._tasks()
        task.setdefault("id", "t" + uuid.uuid4().hex[:7])
        task.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        task.setdefault("status", "open")
        ws.append_row([task.get(k, "") for k in TASK_HEADER], value_input_option="RAW")
        return {k: str(task.get(k, "")) for k in TASK_HEADER}

    def _update_task(self, task_id: str, patch: dict) -> Optional[dict]:
        ws = self._tasks()
        idx = self._task_row(task_id)
        if idx is None:
            return None
        cur = ws.row_values(idx)
        cur += [""] * (len(TASK_HEADER) - len(cur))
        data = {k: cur[i] for i, k in enumerate(TASK_HEADER)}
        data.update({k: v for k, v in patch.items() if k in TASK_HEADER})
        ws.update([[data.get(k, "") for k in TASK_HEADER]], f"A{idx}")
        return data

    def _delete_task(self, task_id: str) -> None:
        idx = self._task_row(task_id)
        if idx and idx > 1:
            self._tasks().delete_rows(idx)

    # ---------- categories ----------
    def _cats(self):
        return self._ws("Categories", CAT_HEADER)

    def _list_cats(self) -> list[dict]:
        ws = self._cats()
        rows = ws.get_all_records(expected_headers=CAT_HEADER, numericise_ignore=["all"])
        if not rows:
            for c in DEFAULT_CATS:
                ws.append_row([c["name"], c["emoji"], c["color"]], value_input_option="RAW")
            return DEFAULT_CATS.copy()
        return [{k: str(r.get(k, "")) for k in CAT_HEADER} for r in rows]

    def _add_cat(self, c: dict) -> dict:
        self._cats().append_row([c.get("name", ""), c.get("emoji", ""), c.get("color", "#888780")],
                                value_input_option="RAW")
        return c

    def _update_cat(self, name: str, patch: dict) -> None:
        ws = self._cats()
        new_name = str(patch.get("name") or name).strip() or name
        for i, v in enumerate(ws.col_values(1), start=1):
            if v == name and i > 1:
                cur = ws.row_values(i); cur += [""] * (3 - len(cur))
                d = {"name": cur[0], "emoji": cur[1], "color": cur[2]}
                for k in CAT_HEADER:
                    if patch.get(k) is not None:
                        d[k] = str(patch[k])
                ws.update([[d["name"], d["emoji"], d["color"]]], f"A{i}")
                break
        if new_name != name:                       # переименование — переносим задачи
            tw = self._tasks()
            col = TASK_HEADER.index("category") + 1
            for i, v in enumerate(tw.col_values(col), start=1):
                if i > 1 and v == name:
                    tw.update_cell(i, col, new_name)

    def _delete_cat(self, name: str) -> None:
        ws = self._cats()
        for i, v in enumerate(ws.col_values(1), start=1):
            if v == name and i > 1:
                ws.delete_rows(i); return

    # ---------- comments ----------
    def _comments(self):
        return self._ws("Comments", COMMENT_HEADER)

    def _list_comments(self, task_id: str) -> list[dict]:
        rows = self._comments().get_all_records(expected_headers=COMMENT_HEADER, numericise_ignore=["all"])
        return [{k: str(r.get(k, "")) for k in COMMENT_HEADER} for r in rows
                if str(r.get("task_id", "")) == task_id]

    def _add_comment(self, task_id: str, text: str) -> dict:
        c = {"id": "c" + uuid.uuid4().hex[:7], "task_id": task_id, "text": text,
             "created_at": datetime.now().isoformat(timespec="seconds")}
        self._comments().append_row([c[k] for k in COMMENT_HEADER], value_input_option="RAW")
        return c

    # ---------- settings ----------
    def _settings_ws(self):
        return self._ws("Settings", SETTINGS_HEADER)

    def _get_settings(self) -> dict:
        ws = self._settings_ws()
        rows = ws.get_all_records(expected_headers=SETTINGS_HEADER, numericise_ignore=["all"])
        stored = {str(r.get("key", "")): str(r.get("value", "")) for r in rows}
        if not stored:
            for k, v in DEFAULT_SETTINGS.items():
                ws.append_row([k, v], value_input_option="RAW")
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **stored}

    def _set_settings(self, patch: dict) -> dict:
        ws = self._settings_ws()
        keys = ws.col_values(1)
        for k, v in patch.items():
            v = str(v)
            if k in keys:
                ws.update([[k, v]], f"A{keys.index(k) + 1}")
            else:
                ws.append_row([k, v], value_input_option="RAW")
                keys.append(k)
        return self._get_settings()

    # ---------- async API ----------
    async def get_settings(self):
        return await asyncio.to_thread(self._get_settings)

    async def set_settings(self, patch):
        return await asyncio.to_thread(self._set_settings, patch)

    async def list_tasks(self):
        return await asyncio.to_thread(self._list_tasks)

    async def add_task(self, task):
        return await asyncio.to_thread(self._add_task, task)

    async def update_task(self, task_id, patch):
        return await asyncio.to_thread(self._update_task, task_id, patch)

    async def delete_task(self, task_id):
        await asyncio.to_thread(self._delete_task, task_id)

    async def list_cats(self):
        return await asyncio.to_thread(self._list_cats)

    async def add_cat(self, c):
        return await asyncio.to_thread(self._add_cat, c)

    async def update_cat(self, name, patch):
        await asyncio.to_thread(self._update_cat, name, patch)

    async def delete_cat(self, name):
        await asyncio.to_thread(self._delete_cat, name)

    async def list_comments(self, task_id):
        return await asyncio.to_thread(self._list_comments, task_id)

    async def add_comment(self, task_id, text):
        return await asyncio.to_thread(self._add_comment, task_id, text)
