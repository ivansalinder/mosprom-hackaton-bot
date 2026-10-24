import csv
from datetime import date
from pathlib import Path
import re
from typing import Any

import aiosqlite

from monitor.config import PROJECT_ROOT, settings
from monitor.db import _db_path


COMMON = """
    e.site AS site,
    e.employee_category AS employee_category,
    e.department AS department,
    e.manager_id AS manager_id
"""


def _week(value: str | None) -> str | None:
    if not value:
        return None
    try:
        current = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return (current.fromordinal(current.toordinal() - current.weekday())).isoformat()


def _mask(value: Any, full_name: str | None) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    if full_name:
        for part in full_name.split():
            if len(part) > 2:
                text = re.sub(re.escape(part), "[name]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", text)
    text = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)", "[phone]", text)
    text = re.sub(r"\b\d{10,12}\b", "[document-number]", text)
    return text


async def _fetch(db: aiosqlite.Connection, sql: str) -> list[dict[str, Any]]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql)
    return [dict(row) for row in await cursor.fetchall()]


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


async def export_analytics(output_dir: Path | None = None) -> list[Path]:
    output_dir = output_dir or PROJECT_ROOT / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(settings.resolved_sqlite_db_path)) as db:
        employees = await _fetch(
            db,
            f"""
            SELECT employee_id, full_name, hire_date, site, employee_category,
                   department, manager_id, recruiter, address_form, status,
                   risk_status, probation_end_date, termination_date,
                   CAST(julianday(COALESCE(termination_date, date('now'))) -
                        julianday(hire_date) AS INTEGER) AS tenure_days,
                   CASE WHEN termination_date IS NOT NULL AND risk_status = 'high'
                        THEN 1 ELSE 0 END AS flag_before_leaving
            FROM employees
            WHERE employee_code IS NOT NULL
            """,
        )
        onboarding = await _fetch(
            db,
            f"""
            SELECT j.id AS event_id, e.employee_id AS employee_id,
                   j.stage_no, j.stage_name, j.stage_key AS stage_point,
                   j.message_type, j.planned_at, j.sent_at,
                   CASE WHEN j.status = 'sent' THEN 1 ELSE 0 END AS delivered,
                   0 AS read, 0 AS replied, NULL AS reply_delay_min,
                   0 AS survey_reminders, {COMMON}
            FROM timeline_jobs j JOIN employees e ON e.id = j.employee_id
            """,
        )
        survey_responses = await _fetch(
            db,
            f"""
            SELECT s.id AS survey_id, e.employee_id AS employee_id,
                   s.survey_point, s.id AS point_order, s.completed_at AS answered_at,
                   ROUND(AVG(a.score_pct), 1) AS adaptation_index_pct,
                   SUM(a.is_low) AS low_answers_cnt, COUNT(a.id) AS questions_cnt,
                   NULL AS sentiment_label, NULL AS sentiment_score,
                   NULL AS main_topic,
                   GROUP_CONCAT(a.comment_text, ' | ') AS free_text_masked,
                   CASE WHEN EXISTS (SELECT 1 FROM red_flags r WHERE r.survey_session_id = s.id)
                        THEN 1 ELSE 0 END AS red_flag,
                   (SELECT flag_type FROM red_flags r WHERE r.survey_session_id = s.id
                    ORDER BY r.id DESC LIMIT 1) AS red_flag_type,
                   {COMMON}
            FROM survey_sessions s
            JOIN employees e ON e.id = s.employee_id
            LEFT JOIN survey_answers a ON a.session_id = s.id
            GROUP BY s.id
            """,
        )
        survey_scores = await _fetch(
            db,
            f"""
            SELECT s.id AS survey_id, e.employee_id AS employee_id,
                   s.survey_point, s.id AS point_order,
                   a.question_id, a.question_text, a.dimension, a.answer_type,
                   a.answer_value AS answer_text, a.score_10, a.score_pct,
                   a.is_low, a.answered_at, {COMMON}
            FROM survey_answers a
            JOIN survey_sessions s ON s.id = a.session_id
            JOIN employees e ON e.id = s.employee_id
            """,
        )
        ai_requests = await _fetch(
            db,
            f"""
            SELECT a.id AS request_id, e.employee_id AS employee_id,
                   a.asked_at, a.topic, a.channel,
                   a.question_text AS question_text_masked,
                   a.retrieval_confidence, a.source_doc,
                   CASE WHEN a.source_doc IS NULL THEN 0 ELSE 1 END AS answer_has_source,
                   a.resolution, a.resolved_by_ai, a.escalation_reason,
                   a.guardrail_blocked, a.user_feedback, a.is_knowledge_gap,
                   a.response_sec, {COMMON}
            FROM ai_requests a JOIN employees e ON e.id = a.employee_id
            """,
        )
        tickets = await _fetch(
            db,
            f"""
            SELECT t.id AS ticket_id, e.employee_id AS employee_id,
                   t.source, t.reason, t.id AS related_id, t.created_at,
                   t.hr_manager, t.first_response_at,
                   CASE WHEN t.first_response_at IS NULL THEN NULL
                        ELSE (julianday(t.first_response_at) - julianday(t.created_at)) * 1440
                   END AS first_response_min,
                   t.resolved_at,
                   CASE WHEN t.resolved_at IS NULL THEN NULL
                        ELSE (julianday(t.resolved_at) - julianday(t.created_at)) * 24
                   END AS resolution_hours,
                   t.status, t.returned_to_bot,
                   CASE WHEN t.first_response_at IS NULL THEN NULL
                        WHEN (julianday(t.first_response_at) - julianday(t.created_at)) * 24
                             <= {int(settings.HR_SLA_HOURS)} THEN 1 ELSE 0 END AS sla_met,
                   {COMMON}
            FROM tickets t JOIN employees e ON e.id = t.employee_id
            """,
        )
        red_flags = await _fetch(
            db,
            f"""
            SELECT r.id AS flag_id, e.employee_id AS employee_id,
                   r.survey_session_id AS survey_id, r.source_point,
                   r.flag_type, r.risk_level, r.detected_at, r.alert_hr_at,
                   r.manager_push_at, r.hr_first_action_at, r.hr_manager,
                   r.status, r.outcome, 0 AS left_company,
                   CASE WHEN r.status = 'open' THEN 1 ELSE 0 END AS is_open,
                   CASE WHEN r.status = 'open'
                        THEN (julianday('now') - julianday(r.detected_at)) * 24
                        ELSE NULL END AS open_hours,
                   {COMMON}
            FROM red_flags r JOIN employees e ON e.id = r.employee_id
            """,
        )
        manager_actions = await _fetch(
            db,
            f"""
            SELECT m.id AS action_id, e.employee_id AS employee_id,
                   m.action_type, m.sent_at, m.due_at, m.acknowledged_at,
                   m.done_at, m.status,
                   CASE WHEN m.done_at IS NOT NULL AND m.due_at IS NOT NULL
                              AND m.done_at > m.due_at THEN 1 ELSE 0 END AS done_late,
                   CASE WHEN m.done_at IS NOT NULL AND m.sent_at IS NOT NULL
                        THEN julianday(m.done_at) - julianday(m.sent_at)
                        ELSE NULL END AS days_to_done,
                   {COMMON}
            FROM manager_actions m JOIN employees e ON e.id = m.employee_id
            """,
        )

    employee_lookup: dict[str, str] = {}
    for row in employees:
        employee_lookup[row["employee_id"]] = row.get("full_name", "")

    exports = {
        "1_employees.csv": (
            employees,
            [
                "employee_id", "hire_date", "site", "employee_category", "department",
                "manager_id", "recruiter", "address_form", "status", "risk_status",
                "probation_end_date", "termination_date", "tenure_days", "flag_before_leaving",
            ],
            "hire_date",
        ),
        "2_onboarding_events.csv": (
            onboarding,
            [
                "event_id", "employee_id", "stage_no", "stage_name", "stage_point",
                "message_type", "planned_at", "sent_at", "delivered", "read", "replied",
                "reply_delay_min", "survey_reminders", "site", "employee_category",
                "department", "manager_id",
            ],
            "sent_at",
        ),
        "3_survey_responses.csv": (
            survey_responses,
            [
                "survey_id", "employee_id", "survey_point", "point_order", "answered_at",
                "adaptation_index_pct", "low_answers_cnt", "questions_cnt", "sentiment_label",
                "sentiment_score", "main_topic", "free_text_masked", "red_flag",
                "red_flag_type", "site", "employee_category", "department", "manager_id",
            ],
            "answered_at",
        ),
        "4_survey_scores.csv": (
            survey_scores,
            [
                "survey_id", "employee_id", "survey_point", "point_order", "question_id",
                "question_text", "dimension", "answer_type", "answer_text", "score_10",
                "score_pct", "is_low", "answered_at", "site", "employee_category",
                "department", "manager_id",
            ],
            "answered_at",
        ),
        "5_ai_requests.csv": (
            ai_requests,
            [
                "request_id", "employee_id", "asked_at", "topic", "channel",
                "question_text_masked", "retrieval_confidence", "source_doc",
                "answer_has_source", "resolution", "resolved_by_ai", "escalation_reason",
                "guardrail_blocked", "user_feedback", "is_knowledge_gap", "response_sec",
                "site", "employee_category", "department", "manager_id",
            ],
            "asked_at",
        ),
        "6_hr_tickets.csv": (
            tickets,
            [
                "ticket_id", "employee_id", "source", "reason", "related_id", "created_at",
                "hr_manager", "first_response_at", "first_response_min", "resolved_at",
                "resolution_hours", "status", "returned_to_bot", "sla_met", "site",
                "employee_category", "department", "manager_id",
            ],
            "created_at",
        ),
        "7_red_flags.csv": (
            red_flags,
            [
                "flag_id", "employee_id", "survey_id", "source_point", "flag_type",
                "risk_level", "detected_at", "alert_hr_at", "manager_push_at",
                "hr_first_action_at", "hr_manager", "status", "outcome", "left_company",
                "is_open", "open_hours", "site", "employee_category", "department", "manager_id",
            ],
            "detected_at",
        ),
        "8_manager_actions.csv": (
            manager_actions,
            [
                "action_id", "employee_id", "action_type", "sent_at", "due_at",
                "acknowledged_at", "done_at", "status", "done_late", "days_to_done",
                "site", "employee_category", "department", "manager_id",
            ],
            "sent_at",
        ),
    }

    paths: list[Path] = []
    for filename, (rows, columns, event_date_field) in exports.items():
        for row in rows:
            row["event_date"] = (
                row.get(event_date_field)[:10]
                if row.get(event_date_field)
                else None
            )
            row["event_week"] = _week(row["event_date"])
            # The source DB keeps real names for operations; exported rows stay pseudonymous.
            name = employee_lookup.get(row.get("employee_id", ""), "")
            for field in ("free_text_masked", "question_text_masked", "answer_text", "reason"):
                if field in row:
                    row[field] = _mask(row[field], name)
        output = output_dir / filename
        _write_csv(output, rows, columns + ["event_date", "event_week"])
        paths.append(output)
    return paths
