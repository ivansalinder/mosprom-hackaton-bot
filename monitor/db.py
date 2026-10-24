from pathlib import Path
import json

import aiosqlite

from monitor.config import settings


def _db_path() -> str:
    return str(settings.resolved_sqlite_db_path)


async def init_db() -> None:
    db_path = settings.resolved_sqlite_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
    async with aiosqlite.connect(str(db_path)) as db:
        if schema_path.exists():
            await db.executescript(schema_path.read_text(encoding="utf-8"))
        cursor = await db.execute("PRAGMA table_info(employees)")
        existing = {row[1] for row in await cursor.fetchall()}
        migrations = {
            "employee_id": "TEXT",
            "employee_code": "TEXT",
            "start_date": "TEXT",
            "site": "TEXT",
            "employee_category": "TEXT DEFAULT 'office'",
            "is_office": "INTEGER NOT NULL DEFAULT 1",
            "manager_id": "TEXT",
            "recruiter": "TEXT",
            "address_form": "TEXT DEFAULT 'ty'",
            "status": "TEXT DEFAULT 'preboarding'",
            "onboarding_state": "TEXT DEFAULT 'unregistered'",
            "handoff_active": "INTEGER NOT NULL DEFAULT 0",
            "probation_end_date": "TEXT",
            "termination_date": "TEXT",
            "risk_status": "TEXT DEFAULT 'normal'",
            "email": "TEXT",
            "welcome_email": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE employees ADD COLUMN {column} {definition}"
                )
        cursor = await db.execute("PRAGMA table_info(red_flags)")
        red_flag_columns = {row[1] for row in await cursor.fetchall()}
        if "hr_manager" not in red_flag_columns:
            await db.execute("ALTER TABLE red_flags ADD COLUMN hr_manager TEXT")
        cursor = await db.execute("PRAGMA table_info(tickets)")
        ticket_columns = {row[1] for row in await cursor.fetchall()}
        if "sla_reminded_at" not in ticket_columns:
            await db.execute("ALTER TABLE tickets ADD COLUMN sla_reminded_at TEXT")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_code "
            "ON employees(employee_code)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_pseudonym "
            "ON employees(employee_id)"
        )
        await db.commit()

    hris_path = settings.resolved_mock_hris_path
    if hris_path.exists():
        employees = json.loads(hris_path.read_text(encoding="utf-8"))
        async with aiosqlite.connect(str(db_path)) as db:
            for employee in employees:
                await db.execute(
                    """
                    INSERT INTO employees (
                        max_user_id, full_name, plant_site, department,
                        manager_max_id, hire_date, employee_code, site,
                        employee_id, employee_category, is_office, start_date,
                        manager_id, recruiter, status,
                        probation_end_date, email
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(employee_id) DO UPDATE SET
                        employee_code = excluded.employee_code,
                        employee_id = excluded.employee_id,
                        full_name = excluded.full_name,
                        plant_site = excluded.plant_site,
                        department = excluded.department,
                        manager_max_id = excluded.manager_max_id,
                        site = excluded.site,
                        employee_category = excluded.employee_category,
                        is_office = excluded.is_office,
                        manager_id = excluded.manager_id,
                        recruiter = excluded.recruiter,
                        email = excluded.email
                    """,
                    (
                        employee.get("max_user_id", f"pending:{employee['employee_id']}"),
                        employee["full_name"],
                        employee["site"],
                        employee.get("department"),
                        employee.get("manager_id"),
                        employee["hire_date"],
                        employee["binding_code"],
                        employee["site"],
                        employee["employee_id"],
                        employee["employee_category"],
                        int(employee.get("is_office", employee["employee_category"] == "office")),
                        employee.get("start_date", employee["hire_date"]),
                        employee.get("manager_id"),
                        employee.get("recruiter"),
                        "preboarding",
                        employee.get("probation_end_date"),
                        employee.get("email"),
                    ),
                )
            await db.commit()


async def log_interaction(
    max_user_id: str,
    interaction_type: str,
    user_text: str,
    bot_response: str,
    topic_tag: str = "общее",
    sentiment: str = "neutral",
    risk_score: float = 0.0,
    is_red_flag: bool = False,
    escalated_to_hr: bool = False,
) -> int:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            """
            INSERT INTO interactions
            (max_user_id, interaction_type, user_text, bot_response, topic_tag, sentiment, risk_score, is_red_flag, escalated_to_hr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                max_user_id,
                interaction_type,
                user_text,
                bot_response,
                topic_tag,
                sentiment,
                risk_score,
                int(is_red_flag),
                int(escalated_to_hr),
            ),
        )
        await db.commit()
        return cursor.lastrowid
