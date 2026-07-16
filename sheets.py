"""Хранилище задач в Google Sheets — та же таблица и колонки, что у бота.
Читает/пишет лист 'Tasks'. gspread синхронный, оборачиваем в to_thread."""
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
HEADER = [
    "id", "title", "notes", "category", "priority",
    "due_at", "remind_at", "recurrence", "status",
    "created_at", "completed_at", "attachments",
    "reminded", "last_nagged_at",
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
        self._ws: Optional[gspread.Worksheet] = None

    def _connect(self) -> gspread.Worksheet:
        if self._ws is not None:
            return self._ws
        client = gspread.authorize(_creds())
        sh = client.open_by_key(self._sheet_id)
        try:
            ws = sh.worksheet("Tasks")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="Tasks", rows=1000, cols=len(HEADER))
        if ws.row_values(1) != HEADER:
            ws.update([HEADER], "A1")
        self._ws = ws
        return ws

    def _list(self) -> list[dict]:
        ws = self._connect()
        rows = ws.get_all_records(expected_headers=HEADER)
        return [{k: str(r.get(k, "")) for k in HEADER} for r in rows]

    def _row_index(self, task_id: str) -> Optional[int]:
        ws = self._connect()
        for i, val in enumerate(ws.col_values(1), start=1):
            if val == task_id:
                return i
        return None

    def _add(self, task: dict) -> dict:
        ws = self._connect()
        task.setdefault("id", uuid.uuid4().hex[:8])
        task.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        task.setdefault("status", "open")
        row = [task.get(k, "") for k in HEADER]
        ws.append_row(row, value_input_option="RAW")
        return {k: str(task.get(k, "")) for k in HEADER}

    def _update(self, task_id: str, patch: dict) -> Optional[dict]:
        ws = self._connect()
        idx = self._row_index(task_id)
        if idx is None:
            return None
        current = ws.row_values(idx)
        current += [""] * (len(HEADER) - len(current))
        data = {k: current[i] for i, k in enumerate(HEADER)}
        data.update({k: v for k, v in patch.items() if k in HEADER})
        ws.update([[data.get(k, "") for k in HEADER]], f"A{idx}")
        return data

    def _delete(self, task_id: str) -> None:
        ws = self._connect()
        idx = self._row_index(task_id)
        if idx and idx > 1:
            ws.delete_rows(idx)

    # async API
    async def list(self) -> list[dict]:
        return await asyncio.to_thread(self._list)

    async def add(self, task: dict) -> dict:
        return await asyncio.to_thread(self._add, task)

    async def update(self, task_id: str, patch: dict) -> Optional[dict]:
        return await asyncio.to_thread(self._update, task_id, patch)

    async def delete(self, task_id: str) -> None:
        await asyncio.to_thread(self._delete, task_id)
