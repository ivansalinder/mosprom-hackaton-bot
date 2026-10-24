from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo

import aiosqlite
from dateutil.relativedelta import relativedelta

from monitor.config import settings
from monitor.db import _db_path
from monitor.stages import is_audience_eligible


logger = logging.getLogger(__name__)

STAGES = [
    ("d_minus_1_ready", 1, "Накануне: готовность", -1, 10, 0),
    ("d_minus_1_docs", 2, "Накануне: документы", -1, 10, 0),
    ("d1_10_congrats", 3, "Первый день: поздравление", 0, 10, 0),
    ("d1_10_email", 4, "Первый день: письмо", 0, 10, 0),
    ("d1_11_map", 5, "Первый день: карта", 0, 11, 0),
    ("d1_12_access", 6, "Первый день: доступы", 0, 12, 0),
    ("d1_1201_postcard", 7, "Первый день: открытка", 0, 12, 1),
    ("d1_1230_lunch", 8, "Первый день: обед", 0, 12, 30),
    ("d1_13_directory", 9, "Первый день: справочник", 0, 13, 0),
    ("d1_15_presentation", 10, "Первый день: презентация", 0, 15, 0),
    ("d1_18_end", 11, "Первый день: итог", 0, 18, 0),
    ("d2_10_selfintro", 12, "Второй день: знакомство", 1, 10, 0),
    ("d3_09_plan", 13, "Третий день: план", 2, 9, 0),
    ("d3_11_gym", 14, "Третий день: спорткомплекс", 2, 11, 0),
    ("d3_1730_survey", 15, "Третий день: опрос", 2, 17, 30),
    ("d10_events", 16, "Мероприятия", 10, 10, 0),
    ("w2_check", 17, "Опрос: две недели", 14, 10, 0),
    ("m2_start", 18, "Начало второго месяца", 1, 9, 0),
    ("m15_reminder_films", 19, "Подборка фильмов", 1, 10, 0),
    ("m17_survey", 20, "Опрос: два месяца", 1, 21, 9),
    ("m3_start", 21, "Начало третьего месяца", 2, 9, 0),
    ("m25_books", 22, "Подборка книг", 2, 14, 10),
    ("final_survey", 23, "Итоговый опрос", 3, 9, 0),
]

FIRST_DAY_STAGES = {
    "d1_10_congrats", "d1_10_email", "d1_11_map", "d1_12_access",
    "d1_1201_postcard", "d1_1230_lunch", "d1_13_directory",
    "d1_15_presentation", "d1_18_end",
}

StageSender = Callable[[dict, str], Awaitable[None]]


def _shift_to_workday(value: datetime, stage_key: str) -> datetime:
    if stage_key in FIRST_DAY_STAGES:
        return value
    if value.weekday() >= 5:
        value += timedelta(days=7 - value.weekday())
        return value.replace(hour=9, minute=0)
    if value.hour < 9 or value.hour > 18:
        return value.replace(hour=9, minute=0)
    return value


def _stage_datetime(start_date: date, stage_key: str, day_offset: int, hour: int, minute: int) -> datetime:
    target_date = start_date + timedelta(days=day_offset)
    if stage_key == "m2_start":
        target_date = start_date + relativedelta(months=1)
    elif stage_key == "m15_reminder_films":
        target_date = start_date + relativedelta(months=1, days=14)
    elif stage_key == "m17_survey":
        target_date = start_date + relativedelta(months=1, days=21)
    elif stage_key == "m3_start":
        target_date = start_date + relativedelta(months=2)
    elif stage_key == "m25_books":
        target_date = start_date + relativedelta(months=2, days=14)
    elif stage_key == "final_survey":
        target_date = start_date + relativedelta(months=3, days=-1)
    return datetime.combine(target_date, time(hour, minute))


class TimelineScheduler:
    def __init__(self, send_stage: StageSender) -> None:
        self.send_stage = send_stage
        self.timezone = ZoneInfo(settings.TIMEZONE)

    async def create_jobs(self, employee: dict) -> int:
        start_date = date.fromisoformat(str(employee.get("start_date") or employee["hire_date"])[:10])
        scale = max(float(settings.TIME_SCALE), 0.0)
        now = datetime.now(self.timezone)
        jobs = []
        for stage_key, stage_no, name, day_offset, hour, minute in STAGES:
            target = _stage_datetime(start_date, stage_key, day_offset, hour, minute)
            target = _shift_to_workday(target, stage_key).replace(tzinfo=self.timezone)
            planned = now + (target - datetime.combine(start_date, time.min, self.timezone)) * scale
            if planned < now:
                planned = now
            jobs.append((employee["id"], stage_key, stage_no, name, "scenario", planned.isoformat()))
        async with aiosqlite.connect(_db_path()) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO timeline_jobs
                    (employee_id, stage_key, stage_no, stage_name,
                     message_type, planned_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                jobs,
            )
            await db.execute(
                """
                UPDATE timeline_jobs SET status = 'disabled'
                WHERE employee_id = ? AND stage_key NOT IN ({})
                  AND status IN ('pending', 'waiting_approval')
                """.format(",".join("?" for _ in STAGES)),
                (employee["id"], *(stage[0] for stage in STAGES)),
            )
            await db.commit()
        return len(jobs)

    async def run_due(self) -> int:
        now = datetime.now(self.timezone).isoformat()
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT j.*, e.max_user_id, e.full_name, e.site, e.plant_site,
                       e.employee_category, e.is_office, e.address_form,
                       e.hire_date, e.handoff_active, e.is_active
                FROM timeline_jobs j JOIN employees e ON e.id = j.employee_id
                WHERE j.status = 'pending' AND j.planned_at <= ?
                ORDER BY j.planned_at, j.stage_no LIMIT 100
                """,
                (now,),
            )
            jobs = [dict(row) for row in await cursor.fetchall()]
        sent = 0
        for job in jobs:
            if not job["is_active"]:
                await self._set_status(job["id"], "cancelled")
                continue
            if job["handoff_active"]:
                continue
            if not is_audience_eligible(job["stage_key"], job):
                await self._set_status(job["id"], "skipped")
                continue
            if job["stage_key"] in {"d2_10_selfintro", "d3_09_plan"}:
                approved = await self._is_approved(job)
                if not approved and not settings.DEMO_MODE:
                    await self._set_status(job["id"], "waiting_approval")
                    continue
            await self._set_status(job["id"], "sent")
            try:
                await self.send_stage(job, job["stage_key"])
            except Exception:
                logger.exception(
                    "Timeline stage failed after being marked sent: employee=%s stage=%s",
                    job["employee_id"],
                    job["stage_key"],
                )
                await self._set_status(job["id"], "failed")
                continue
            sent += 1
        return sent

    async def _is_approved(self, job: dict) -> bool:
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                "SELECT 1 FROM recruiter_approvals WHERE employee_id = ? AND stage_key = ?",
                (job["employee_id"], job["stage_key"]),
            )
            return await cursor.fetchone() is not None

    async def simulate(self, employee: dict, stage_no: int) -> int:
        selected = [item for item in STAGES if item[1] == stage_no]
        for stage_key, _, _, _, _, _ in selected:
            if is_audience_eligible(stage_key, employee):
                await self.send_stage(employee, stage_key)
        return len(selected)

    async def _set_status(self, job_id: int, status: str) -> None:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                UPDATE timeline_jobs
                SET status = ?,
                    sent_at = CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE sent_at END
                WHERE id = ?
                """,
                (status, status, job_id),
            )
            await db.commit()
