import json
import unicodedata
from typing import Any

import aiosqlite

from monitor.db import _db_path


async def get_employee(user_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM employees WHERE max_user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_employee_by_code(code: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFC", code.strip()).casefold().replace("ё", "е")
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM employees WHERE employee_code IS NOT NULL"
        )
        for row in await cursor.fetchall():
            candidate = unicodedata.normalize(
                "NFC", row["employee_code"].strip()
            ).casefold().replace("ё", "е")
            if normalized == candidate:
                return dict(row)
        return None


async def link_employee(employee_id: int, user_id: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            UPDATE employees
            SET max_user_id = ?, onboarding_state = 'choose_address_form'
            WHERE id = ?
            """,
            (user_id, employee_id),
        )
        await db.commit()


async def get_state(user_id: str) -> tuple[str | None, dict[str, Any]]:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "SELECT state, data_json FROM conversation_states WHERE max_user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None, {}
        return row[0], json.loads(row[1])


async def set_state(
    user_id: str, state: str | None, data: dict[str, Any] | None = None
) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        if state is None:
            await db.execute(
                "DELETE FROM conversation_states WHERE max_user_id = ?",
                (user_id,),
            )
        else:
            await db.execute(
                """
                INSERT INTO conversation_states (max_user_id, state, data_json)
                VALUES (?, ?, ?)
                ON CONFLICT(max_user_id) DO UPDATE SET
                    state = excluded.state,
                    data_json = excluded.data_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, state, json.dumps(data or {}, ensure_ascii=False)),
            )
        await db.commit()


async def log_message(
    user_id: str,
    direction: str,
    text: str,
    *,
    stage_key: str | None = None,
    message_type: str = "dialogue",
    payload: dict[str, Any] | None = None,
) -> None:
    employee = await get_employee(user_id)
    employee_id = employee["id"] if employee else None
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO messages_log
                (employee_id, direction, stage_key, message_type, text, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                employee_id,
                direction,
                stage_key,
                message_type,
                text,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )
        await db.commit()


async def create_ticket(
    employee_id: int,
    source: str,
    reason: str,
    context: str = "",
) -> int:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            """
            INSERT INTO tickets (employee_id, source, reason, context)
            VALUES (?, ?, ?, ?)
            """,
            (employee_id, source, reason, context),
        )
        await db.execute(
            "UPDATE employees SET handoff_active = 1 WHERE id = ?",
            (employee_id,),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def close_open_ticket(employee_id: int) -> bool:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            """
            UPDATE tickets
            SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
            WHERE employee_id = ? AND status = 'open'
            """,
            (employee_id,),
        )
        if cursor.rowcount:
            await db.execute(
                "UPDATE employees SET handoff_active = 0 WHERE id = ?",
                (employee_id,),
            )
            await db.commit()
            return True
        await db.commit()
        return False
