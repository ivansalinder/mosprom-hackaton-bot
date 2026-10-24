from datetime import datetime, timedelta
import asyncio
import json
import logging
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import yaml
from dateutil.relativedelta import relativedelta

from monitor.bot.max_adapter import IncomingCallback, IncomingMessage, MaxAdapter
from monitor.config import settings
from monitor.db import _db_path
from monitor.storage import (
    close_open_ticket,
    create_ticket,
    get_employee,
    get_employee_by_code,
    get_state,
    link_employee,
    log_message,
    set_state,
)
from monitor.rag.engine import RAGEngine
from monitor.stages import STAGE_HANDLERS, is_audience_eligible
from monitor.timeline import STAGES, TimelineScheduler


logger = logging.getLogger(__name__)
DOCUMENTS = [
    "Бумажная трудовая книжка и сведения о трудовой деятельности (можно выгрузить с Госуслуг)",
    "Паспорт",
    "СНИЛС",
    "ИНН",
    "Документ об образовании (диплом, свидетельства, удостоверения, допуски)",
    "Свидетельство о браке или разводе (при смене ФИО)",
    "Свидетельство о рождении ребёнка (до 18 лет)",
    "Документы воинского учёта",
]

SURVEY_QUESTIONS: dict[str, dict[str, Any]] = {}
PLAYLIST_URL = "https://music.yandex.ru/artist/5631933?lang=en"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required content file does not exist: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Content file must contain a YAML mapping: {path}")
    return loaded


class _SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _mask_text(text: str) -> str:
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", text)
    text = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)", "[phone]", text)
    text = re.sub(r"\b\d{10,12}\b", "[document-number]", text)
    return text


class KorsikFlows:
    def __init__(self, adapter: MaxAdapter) -> None:
        self.adapter = adapter
        self.messages = _load_yaml(settings.resolved_content_path)
        self.scenario_texts = _load_yaml(
            settings.resolved_content_path.with_name("scenario_texts.yaml")
        )
        self.survey_definitions = _load_yaml(
            settings.resolved_content_path.with_name("surveys.yaml")
        )
        self.timeline = TimelineScheduler(self.send_stage)
        self.rag_engine = RAGEngine()
        self.pre_block_delay = 2
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _message(self, key: str, employee: dict | None = None, **values: Any) -> str:
        if "." in key:
            section, name = key.split(".", 1)
            template = self.messages[section][name]
        else:
            template = self.messages[key]
        return self._format_message(template, employee, **values)

    def _scenario_message(
        self, text_id: str, employee: dict | None = None, **values: Any
    ) -> str:
        template = self.scenario_texts[text_id]
        context = {
            "name": employee.get("full_name", "сотрудник") if employee else "сотрудник",
            "full_name": employee.get("full_name", "сотрудник") if employee else "сотрудник",
            "sla_hours": settings.HR_SLA_HOURS,
            "recruiter_url": settings.RECRUITER_URL,
            **values,
        }
        return template.format_map(_SafeFormat(context))

    @staticmethod
    def _format_message(
        template: str | dict[str, str], employee: dict | None = None, **values: Any
    ) -> str:
        if isinstance(template, str):
            text = template
        else:
            form = employee.get("address_form", "ty") if employee else "ty"
            text = template.get(form, template.get("ty", ""))
        context = {
            "name": employee.get("full_name", "сотрудник") if employee else "сотрудник",
            "site": employee.get("site", "площадка") if employee else "площадка",
            "hire_date": employee.get("hire_date", "") if employee else "",
            "destination": (
                "офиса" if employee and employee.get("site") != "GTP" else "предприятия"
            ),
            **values,
        }
        return text.format_map(_SafeFormat(context))

    async def _send(
        self,
        user_id: str,
        text: str,
        rows: list[list[tuple[str, str]]] | None = None,
        *,
        stage_key: str | None = None,
        message_type: str = "dialogue",
    ) -> None:
        chunks = self._split_message(text)
        for index, chunk in enumerate(chunks):
            await self.adapter.send(
                user_id, chunk, rows if index == len(chunks) - 1 else None
            )
            await log_message(
                user_id,
                "out",
                chunk,
                stage_key=stage_key,
                message_type=message_type,
            )

    @staticmethod
    def _split_message(text: str, limit: int = 3500) -> list[str]:
        chunks: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            while len(paragraph) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) > limit:
                chunks.append(current)
                current = paragraph
            else:
                current = candidate
        if current or not chunks:
            chunks.append(current)
        return chunks

    async def _send_asset(self, user_id: str, asset_name: str) -> None:
        await self.adapter.send_asset_or_stub(user_id, asset_name)
        await log_message(
            user_id,
            "out",
            f"[ASSET: {asset_name}]",
            message_type="Информ",
        )

    @staticmethod
    def _help_buttons(context: str, *, relay: bool = True) -> list[list[tuple[str, str]]]:
        rows: list[list[tuple[str, str]]] = []
        if relay:
            rows.append([("📨 Передать через Корсика", f"help:relay:{context}")])
        rows.append([("💬 Связаться с рекрутером", f"help:contact:{context}")])
        return rows

    async def _send_playlist(self, user_id: str, text: str) -> None:
        send_link = getattr(self.adapter, "send_link_button", None)
        if send_link is None:
            await self._send(user_id, f"{text}\n{PLAYLIST_URL}", message_type="Опрос")
        else:
            await send_link(user_id, text, "🎶 Открыть плейлист", PLAYLIST_URL)
            await log_message(user_id, "out", text, message_type="Опрос")

    async def start(self, event: IncomingMessage) -> None:
        employee = await get_employee(event.user_id)
        if employee and employee["onboarding_state"] == "registered":
            await self._send(
                event.user_id,
                self._scenario_message("P01_welcome", employee),
                self._menu(),
                stage_key="registration.menu",
                message_type="Информ+диалог",
            )
            return

        state, _ = await get_state(event.user_id)
        if state == "awaiting_code":
            code = event.text.split(maxsplit=1)
            if len(code) > 1:
                await self._check_binding_code(event.user_id, code[1])
            else:
                await self._send(event.user_id, self.messages["bind"]["ask_code"])
            return
        if state:
            await self._send(
                event.user_id,
                self._scenario_message("expect_text_hint"),
                stage_key="registration.in_progress",
            )
            return

        await set_state(event.user_id, "awaiting_code", {})
        code = event.text.split(maxsplit=1)
        if len(code) > 1:
            await self._check_binding_code(event.user_id, code[1])
        else:
            await self._send(event.user_id, self.messages["bind"]["ask_code"], stage_key="registration.start")

    async def message(self, event: IncomingMessage) -> None:
        text = event.text.strip()
        if not text and not event.attachments:
            return
        employee = await get_employee(event.user_id)
        await log_message(
            event.user_id,
            "in",
            _mask_text(text) if text else "[получено вложение]",
            message_type="Действие пользователя",
        )

        command = text.split(maxsplit=1)[0].lower() if text else ""
        if command == "/start":
            await self.start(event)
            return
        if command == "/food":
            if employee:
                await self._send(
                    event.user_id,
                    self._scenario_message("D1_1230_lunch_yes", employee),
                    stage_key="command.food",
                )
                await self._send_asset(event.user_id, f"lunch_{employee['site']}.pdf")
            else:
                await self._send(event.user_id, self._message("commands.food"))
            return
        if command == "/hr":
            await self._handoff(event.user_id, text, "Команда /HR", "команда")
            return
        if command == "/close":
            await self._close_handoff(event.user_id, text)
            return
        if command == "/export_analytics":
            await self._export_analytics(event.user_id)
            return
        if command == "/simulate":
            await self._simulate(event.user_id, text)
            return
        if command == "/approve":
            await self._approve_stage(event.user_id, text)
            return
        if command == "/help":
            await self._send_help(event.user_id)
            return
        if command == "/stages":
            await self._show_stages(event.user_id)
            return
        if command == "/next":
            await self._run_next_stage(event.user_id)
            return
        if command == "/jump":
            await self._jump_stage(event.user_id, text)
            return
        if command == "/reset":
            await self._reset_employee(event.user_id)
            return
        if command == "/me":
            await self._show_profile(event.user_id)
            return
        if command == "/stats":
            await self._show_stats(event.user_id)
            return

        state, state_data = await get_state(event.user_id)
        if state == "awaiting_code":
            await self._check_binding_code(event.user_id, text)
            return
        if state == "pre_self_intro":
            await self._finish_self_intro(
                event.user_id, text, state_data, event.attachments
            )
            return
        if state == "help_relay":
            await self._finish_help_relay(event.user_id, text, state_data)
            return
        if state == "survey_text":
            await self._save_survey_text(event.user_id, text, state_data)
            return
        if state == "survey_reason":
            await self._save_survey_reason(event.user_id, text, state_data)
            return
        if state == "self_intro":
            await self._finish_self_intro(
                event.user_id, text, state_data, event.attachments
            )
            return
        if state in {"survey", "survey_bye"}:
            await self._send(
                event.user_id,
                self._message("help.expect_text_hint", employee),
            )
            return

        if not employee:
            await self.start(event)
            return
        if employee["handoff_active"]:
            await self.adapter.send_hr_notice(
                f"Новое сообщение в активном handoff ({employee['employee_code']}): "
                f"{_mask_text(text)}",
                fallback_user_id=event.user_id,
            )
            return

        if event.attachments:
            await self._send(
                event.user_id,
                self._message("commands.stale_button", employee),
            )
            return
        await self._offer_free_question(event.user_id, employee, text)

    async def callback(self, event: IncomingCallback) -> None:
        user_id, payload = event.user_id, event.payload
        employee = await get_employee(user_id)
        state, _ = await get_state(user_id)
        if payload.startswith("help:contact:"):
            text = (
                self._message("help.contact_link", employee, recruiter_url=settings.RECRUITER_URL)
                if settings.RECRUITER_URL
                else self._message("help.contact_stub", employee)
            )
            await self._send(user_id, text, stage_key="help.contact")
            return
        if payload.startswith("help:relay:"):
            _, _, context = payload.split(":", 2)
            _, state_data = await get_state(user_id)
            original_question = (
                state_data.get("question", "")
                if state == "free_question_offer"
                else ""
            )
            resume_state = (
                state
                if state in {"survey", "survey_text", "survey_reason"}
                else None
            )
            await set_state(
                user_id,
                "help_relay",
                {
                    "context": context,
                    "original_question": original_question,
                    "resume_state": resume_state,
                    "resume_data": state_data if resume_state else {},
                },
            )
            await self._send(user_id, self._message("help.relay_prompt", employee))
            return
        if state == "survey_bye" and payload in {"bye:1", "bye:2"}:
            await self._finish_final_survey(user_id, payload)
            return
        if state == "survey_reason" and payload in {
            "reason:skip",
            "survey:reason:recruiter",
        }:
            _, state_data = await get_state(user_id)
            await self._save_survey_reason(user_id, "", state_data)
            if payload == "survey:reason:recruiter":
                text = (
                    self._message("help.contact_link", employee, recruiter_url=settings.RECRUITER_URL)
                    if settings.RECRUITER_URL
                    else self._message("help.contact_stub", employee)
                )
                await self._send(user_id, text, stage_key="survey.reason.contact")
            return
        if payload.startswith("survey:start:"):
            survey_key = payload.rsplit(":", 1)[-1]
            points = {
                "survey_day3": "day3",
                "survey_2weeks": "2weeks",
                "survey_month2": "2months",
                "survey_final": "end",
            }
            if employee and survey_key in points:
                state, data = await get_state(user_id)
                if state == "survey" and data.get("point") == points[survey_key]:
                    await self._send_survey_question(user_id)
                else:
                    await self._start_survey(user_id, employee, points[survey_key])
            return
        if state == "confirm_employee" and payload.startswith("reg:"):
            await self._registration_callback(user_id, payload)
            return
        if state == "pre_address" and payload in {"addr:ty", "addr:vy"}:
            await self._continue_pre_block(user_id, payload.rsplit(":", 1)[-1])
            return
        if state == "pre_transport" and payload in {"transport:yes", "transport:no"}:
            await self._finish_pre_block(user_id, payload)
            return
        if state == "pre_self_intro" and payload == "selfintro:skip":
            await self._finish_self_intro(user_id, "", {"skipped": True})
            return
        if employee and employee["handoff_active"]:
            return
        if payload.startswith("reg:"):
            await self._registration_callback(user_id, payload)
        elif payload.startswith("survey:"):
            await self._survey_callback(user_id, payload)
        elif payload == "hr:contact":
            text = (
                self._message("help.contact_link", employee, recruiter_url=settings.RECRUITER_URL)
                if settings.RECRUITER_URL
                else self._message("help.contact_stub", employee)
            )
            await self._send(user_id, text, stage_key="help.contact")
        elif payload in {"lunch:yes", "food:yes"}:
            await self._send(
                user_id,
                self._scenario_message("D1_1230_lunch_yes", employee),
                stage_key="day1.lunch",
            )
            await self._send_asset(user_id, f"lunch_{employee['site']}.pdf")
        elif payload in {"lunch:no", "food:no"}:
            await self._send(
                user_id,
                self._scenario_message("D1_1230_lunch_no", employee),
                stage_key="day1.lunch",
            )
        elif payload == "transport:yes":
            await self._send(
                user_id,
                self._message("preboarding.transport_stub", employee),
                stage_key="preboarding.transport.answer",
            )
        elif payload == "transport:later":
            await self._schedule_transport_reminder(employee)
            await self._send(
                user_id,
                "Хорошо, напомню позже.",
                stage_key="preboarding.transport.later",
            )
        elif payload.startswith("daybefore:"):
            key = (
                "timeline.day_before_ready"
                if payload == "daybefore:ready"
                else "timeline.day_before_worried"
            )
            employee = await get_employee(user_id)
            await self._send(user_id, self._message(key, employee), stage_key="day_before.answer")
        elif payload in {"map:yes", "map:no"}:
            answer = self._scenario_message(
                "D1_11_map_answer" if payload == "map:yes" else "D1_11_map_no",
                employee,
            )
            await self._send(user_id, answer, stage_key="day1.map.answer")
        elif payload in {"gym:yes", "gym:no"}:
            text = self._scenario_message(
                "D3_11_gym_answer" if payload == "gym:yes" else "D3_11_gym_no",
                employee,
            )
            await self._send(user_id, text, stage_key="day3.gym.answer")
        elif payload in {"m2:p1", "m2:p2", "m2:p3"}:
            text_id = "M2_2_bad" if payload == "m2:p3" else "M2_2_ok"
            rows = self._help_buttons("m2_adaptation") if payload == "m2:p3" else None
            await self._send(
                user_id,
                self._scenario_message(text_id, employee),
                rows,
                stage_key="m2.adaptation.answer",
            )
        elif payload == "access:no":
            await self._send(
                user_id,
                self._scenario_message("D1_12_access_no", employee),
                stage_key="day1.access.answer",
            )
            await self._handoff(
                user_id,
                "Нет доступов или материалов",
                "Нет доступов (уведомление HR/IT — заглушка)",
                "access",
                notify_employee=False,
            )
        elif payload == "access:yes":
            await self._send(
                user_id,
                self._scenario_message("D1_12_access_yes", employee),
                stage_key="day1.access.answer",
            )
        elif payload in {"feedback:positive", "feedback:negative"}:
            if employee:
                async with aiosqlite.connect(_db_path()) as db:
                    await db.execute(
                        """
                        UPDATE ai_requests SET user_feedback = ?
                        WHERE id = (
                            SELECT id FROM ai_requests WHERE employee_id = ?
                            ORDER BY id DESC LIMIT 1
                        )
                        """,
                        ("positive" if payload.endswith("positive") else "negative", employee["id"]),
                    )
                    await db.commit()
            await self._send(user_id, "Спасибо за обратную связь!")
        elif payload in {"menu:about", "menu:help"}:
            text = (
                "Я Корсик, цифровой гид по адаптации в Группе ПОЛИПЛАСТИК."
                if payload == "menu:about"
                else "Доступны /Food и /HR. Остальные функции пока в демо-режиме."
            )
            await self._send(user_id, text, self._menu())
        elif payload == "menu:back":
            await self._send(user_id, "Главное меню:", self._menu())
        else:
            await self._send(user_id, self._message("commands.stale_button", employee))

    async def bot_started(self, event: IncomingMessage) -> None:
        await self.start(event)

    async def _send_stage_legacy(self, job: dict, stage_key: str) -> None:
        user_id = str(job["max_user_id"])
        employee = job
        site = employee.get("site") or employee.get("plant_site")
        category = employee.get("employee_category") or "office"
        if stage_key.startswith("approval_needed:"):
            stage = stage_key.split(":", 1)[1]
            await self.adapter.send_hr_notice(
                f"Нет отмашки рекрутера для этапа {stage} у {employee['employee_code']}. "
                f"Используйте /approve {employee['employee_code']} "
                f"{'day2' if stage == 'self_intro' else 'day3'}.",
                fallback_user_id=user_id,
            )
            return
        if stage_key.startswith("transport_reminder_"):
            template = (
                self.messages["preboarding"]["transport_by_site"].get(site)
                or self.messages["preboarding"]["transport"]
            )
            await self._send(
                user_id,
                self._format_message(template, employee),
                [[("Да", "transport:yes"), ("Позже", "transport:later")]],
                stage_key=stage_key,
                message_type="Отложенное уведомление",
            )
            return
        stage_messages = {
            "preboarding_documents": (
                "preboarding.documents",
                {"documents": self._document_list(site)},
                None,
            ),
            "preboarding_transport": (
                "preboarding.transport",
                {},
                [[("Да", "transport:yes"), ("Позже", "transport:later")]],
            ),
            "preboarding_facts": (
                "preboarding.fact_1",
                {},
                None,
            ),
            "preboarding_finish": ("preboarding.finish", {}, None),
            "day_before": (
                "timeline.day_before",
                {},
                [[
                    ("Да, жду не дождусь!", "daybefore:ready"),
                    ("Готов/Готова", "daybefore:ready"),
                    ("Немного волнуюсь", "daybefore:worried"),
                ]],
            ),
            "day_one": ("timeline.day_one", {}, None),
            "map": (
                "timeline.map_prompt",
                {},
                [[("Да", "map:yes"), ("Нет", "map:no")]],
            ),
            "access": (
                "timeline.access",
                {},
                [[("Да, всё есть", "access:yes"), ("Нет", "access:no")]],
            ),
            "postcard": ("timeline.postcard", {}, None),
            "lunch": (
                "timeline.lunch",
                {},
                [[("Да!", "food:yes"), ("Нет, спасибо!", "food:no")]],
            ),
            "contacts": ("timeline.contacts", {}, None),
            "company_presentation": ("timeline.presentation", {}, None),
            "day_one_summary": ("timeline.day_one_summary", {}, None),
            "self_intro": ("timeline.self_intro", {}, None),
            "adaptation_plan": ("timeline.adaptation_plan", {}, None),
            "gym": ("timeline.gym", {}, [[("Да", "gym:yes"), ("Нет", "gym:no")]]),
            "events": ("timeline.events", {}, None),
            "manager_meeting": ("timeline.manager_meeting", {}, None),
            "finish_line": ("timeline.finish_line", {}, None),
            "books": ("timeline.books", {}, None),
        }

        if stage_key in {"survey_day3", "survey_2weeks", "survey_month", "survey_2months", "survey_end"}:
            point = {
                "survey_day3": "day3",
                "survey_2weeks": "2weeks",
                "survey_month": "month",
                "survey_2months": "2months",
                "survey_end": "end",
            }[stage_key]
            await self._start_survey(user_id, employee, point)
            return
        if stage_key == "preboarding_transport":
            template = (
                self.messages["preboarding"]["transport_by_site"].get(site)
                or self.messages["preboarding"]["transport"]
            )
            await self._send(
                user_id,
                self._format_message(template, employee),
                [[("Да", "transport:yes"), ("Позже", "transport:later")]],
                stage_key=stage_key,
                message_type="Информ+диалог",
            )
            return
        if stage_key == "map" and site == "GrPP":
            await self._send(
                user_id,
                "[TODO: уточнить, нужно ли показывать карту для GrPP; пока шаг пропущен.]",
                stage_key=stage_key,
                message_type="Информ",
            )
            return
        if stage_key == "day_one_summary" and category != "office":
            return
        if stage_key == "adaptation_plan" and category != "office":
            return
        if stage_key == "gym" and site != "GTP":
            return
        if stage_key == "events" and site not in {"GrPP", "Luzhniki"}:
            return
        if stage_key == "books" and category != "office":
            return
        if stage_key == "self_intro":
            await set_state(user_id, "self_intro", {})
            await self._send(
                user_id,
                self._message("timeline.self_intro", employee),
                stage_key=stage_key,
                message_type="Диалог",
            )
            return
        if stage_key == "manager_meeting":
            await self._add_manager_action(employee)
        key, values, rows = stage_messages.get(
            stage_key,
            ("timeline.fallback", {}, None),
        )
        if stage_key == "preboarding_documents" and site == "GTP":
            values["documents"] += "\n9. Фотографии 3×4, 2 шт. (цветные или чёрно-белые)"
        if stage_key == "contacts":
            link = "http://ats/" if site == "GTP" else "https://my.polyplastic.ru/employees/Pages/address-book.aspx"
            key = "timeline.contacts"
            await self._send(
                user_id,
                f"{self._message(key, employee)}\n{link}\n{self._message('timeline.contacts_note', employee)}",
                stage_key=stage_key,
                message_type="Информ",
            )
            return
        if stage_key == "day_before":
            message = self._message(key, employee, **values)
            message += "\n\nНапоминаю список документов:\n" + self._document_list(site)
            if site == "GTP":
                message += "\n9. Фотографии 3×4, 2 шт. (цветные или чёрно-белые)"
            message += "\n[TODO: assets/img_3.png]"
            await self._send(
                user_id,
                message,
                rows,
                stage_key=stage_key,
                message_type="Информ+диалог",
            )
            return
        if stage_key == "preboarding_facts":
            for fact in ("preboarding.fact_1", "preboarding.fact_2", "preboarding.fact_3"):
                await self._send(user_id, self._message(fact, employee), stage_key=stage_key, message_type="Информ")
            return
        if stage_key in {"postcard", "company_presentation", "adaptation_plan", "events", "finish_line", "books"}:
            await self._send(
                user_id,
                f"{self._message(key, employee, **values)}\n[TODO: файл вложения по ТЗ: assets/{stage_key}.]",
                rows,
                stage_key=stage_key,
                message_type="Информ",
            )
            return
        await self._send(
            user_id,
            self._message(key, employee, **values),
            rows,
            stage_key=stage_key,
            message_type="Информ+диалог" if rows else "Информ",
        )

    async def send_stage(self, job: dict, stage_key: str) -> None:
        handler = STAGE_HANDLERS.get(stage_key)
        if handler is None:
            raise ValueError(f"Unknown timeline stage: {stage_key}")
        await handler(self, job)

    async def _run_scenario_stage(self, stage_key: str, employee: dict) -> None:
        user_id = str(employee["max_user_id"])
        site = employee.get("site", employee.get("plant_site"))
        if not is_audience_eligible(stage_key, employee):
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    """
                    UPDATE timeline_jobs SET status = 'skipped'
                    WHERE employee_id = ? AND stage_key = ?
                    """,
                    (employee["id"], stage_key),
                )
                await db.commit()
            return
        messages: dict[str, tuple[list[str], list[list[tuple[str, str]]], list[str]]] = {
            "d_minus_1_ready": (["D1M_ready_q"], [[("Да, жду не дождусь!", "ready:1"), ("Готов/Готова", "ready:2"), ("Да, но немного волнуюсь", "ready:3")]], []),
            "d_minus_1_docs": (["D1M_docs_reminder", "P06_docs_list_gtp" if site == "GTP" else "P06_docs_list_grpp_luzhniki"], [], ["img_3.png"]),
            "d1_10_congrats": (["D1_10_congrats", "D1_10_access_stub"], [], ["img_4.png"]),
            "d1_10_email": (["D1_10_email"], [], []),
            "d1_11_map": ([f"D1_11_map_q_{site.lower()}"], [[("Да, покажи карту", "map:yes"), ("Не сейчас", "map:no")]], []),
            "d1_12_access": (["D1_12_access_q_gtp" if site == "GTP" else "D1_12_access_q_grpp_luzhniki"], [[("Да, все есть", "access:yes"), ("Нет", "access:no")]], []),
            "d1_1201_postcard": (["D1_1201_postcard"], [], ["postcard_1.png"]),
            "d1_1230_lunch": (["D1_1230_lunch_q"], [[("Да!", "lunch:yes"), ("Нет, спасибо!", "lunch:no")]], []),
            "d1_13_directory": (["D1_13_directory_1", "D1_13_directory_2_gtp" if site == "GTP" else "D1_13_directory_2_grpp_luzhniki"], [], []),
            "d1_15_presentation": (["D1_15_presentation"], [], ["presentation.pdf"]),
            "d1_18_end": (["D1_18_end"], [], ["img_10.png"]),
            "d2_10_selfintro": (["D2_10_selfintro"], [[("Пропустить", "selfintro:skip")]], ["self_intro_example.png"]),
            "d3_09_plan": (["D3_09_plan_1", "D3_09_plan_2"], [], ["adaptation_plan.pdf"]),
            "d3_11_gym": (["D3_11_gym_q"], [[("Да, расскажи", "gym:yes"), ("Не сейчас", "gym:no")]], []),
            "d3_1730_survey": (["D3_1730_invite"], [[("▶️ Начать опрос", "survey:start:survey_day3")]], []),
            "d10_events": (["D10_1", "D10_2", "D10_3", "D10_4"], [], ["events_1.jpg", "events_2.jpg"]),
            "w2_check": (["W2_intro"], [[("▶️ Начать опрос", "survey:start:survey_2weeks")]], ["img_14.png"]),
            "m2_start": (["M2_1", "M2_2_q"], [[("Все хорошо! Продвигаюсь по плану", "m2:p1"), ("Все ок! Немного отстаю от графика", "m2:p2"), ("Я отстаю от графика", "m2:p3")]], ["img_10.png", "img_7.png"]),
            "m15_reminder_films": (["M15_1", "M15_2_films"], [], ["img_15.png"]),
            "m17_survey": (["M17_1", "M17_2"], [[("▶️ Начать опрос", "survey:start:survey_month2")]], ["img_8.png"]),
            "m3_start": (["M3_1"], [], ["img_10.png"]),
            "m25_books": (["M25_books"], [], ["postcard_2.png"]),
            "final_survey": (["F_1"], [[("▶️ Начать опрос", "survey:start:survey_final")]], ["img_10.png"]),
        }
        message_ids, rows, assets_name = messages[stage_key]
        if stage_key == "d1_10_email":
            logger.info("EMAIL STUB: welcome email for %s", employee["employee_id"])
        if stage_key == "d2_10_selfintro":
            await set_state(user_id, "pre_self_intro", {"stage_key": stage_key})
        for index, message_id in enumerate(message_ids):
            text = self._scenario_message(message_id, employee)
            await self._send(
                user_id,
                text,
                rows if index == len(message_ids) - 1 else None,
                stage_key=stage_key,
                message_type="Опрос" if "survey" in stage_key else "Информ+диалог" if rows else "Информ",
            )
            if index < len(message_ids) - 1:
                await asyncio.sleep(2)
        for asset_name in assets_name:
            await self._send_asset(user_id, asset_name)
        auto_survey = {"m17_survey": "2months", "final_survey": "end"}.get(stage_key)
        if auto_survey:
            task = asyncio.create_task(
                self._start_survey_after_delay(user_id, employee, auto_survey)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_task_finished)

    def _background_task_finished(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "Delayed survey start failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _start_survey_after_delay(
        self, user_id: str, employee: dict, point: str
    ) -> None:
        await asyncio.sleep(max(0.0, 60.0 * float(settings.TIME_SCALE)))
        current_employee = await get_employee(user_id)
        if (
            current_employee
            and current_employee["is_active"]
            and not current_employee["handoff_active"]
        ):
            await self._start_survey(user_id, employee, point)

    async def _begin_pre_block(self, user_id: str) -> None:
        employee = await get_employee(user_id)
        if not employee:
            raise RuntimeError("Bound employee profile was not found for pre-block")
        await set_state(user_id, "pre_address", {})
        for message_id in ("P01_welcome", "P02_what_can"):
            await self._send(user_id, self._scenario_message(message_id, employee), stage_key="pre_block")
            await asyncio.sleep(self.pre_block_delay)
        await self._send(
            user_id,
            self._scenario_message("P03_address_question", employee),
            [[("На ТЫ", "addr:ty"), ("На ВЫ", "addr:vy")]],
            stage_key="pre_block",
        )

    async def _continue_pre_block(self, user_id: str, form: str) -> None:
        if form not in {"ty", "vy"}:
            return
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute("UPDATE employees SET address_form = ? WHERE max_user_id = ?", (form, user_id))
            await db.commit()
        employee = await get_employee(user_id)
        if not employee:
            raise RuntimeError("Bound employee profile disappeared during pre-block")
        await self._send(user_id, self._scenario_message(f"P04_nice_{form}", employee), stage_key="pre_block")
        for message_id in ("P05_intro", "P06_docs_intro"):
            await asyncio.sleep(self.pre_block_delay)
            await self._send(user_id, self._scenario_message(message_id, employee), stage_key="pre_block")
        docs_id = "P06_docs_list_gtp" if employee["site"] == "GTP" else "P06_docs_list_grpp_luzhniki"
        await asyncio.sleep(self.pre_block_delay)
        await self._send(user_id, self._scenario_message(docs_id, employee), stage_key="pre_block")
        await asyncio.sleep(self.pre_block_delay)
        await set_state(user_id, "pre_transport", {})
        await self._send(
            user_id,
            self._scenario_message(f"P07_transport_q_{employee['site'].lower()}", employee),
            [[("Да, расскажи", "transport:yes"), ("Не сейчас", "transport:no")]],
            stage_key="pre_block",
        )

    async def _finish_pre_block(self, user_id: str, payload: str) -> None:
        employee = await get_employee(user_id)
        if not employee:
            raise RuntimeError("Bound employee profile disappeared during pre-block")
        answer_id = "P07_transport_answer" if payload == "transport:yes" else "P07_transport_no"
        await self._send(user_id, self._scenario_message(answer_id, employee), stage_key="pre_block")
        for message_id in ("P08_facts", "P09_pre_bye"):
            await asyncio.sleep(self.pre_block_delay)
            await self._send(user_id, self._scenario_message(message_id, employee), stage_key="pre_block")
        start_date = datetime.now(ZoneInfo(settings.TIMEZONE)).date() + timedelta(days=3)
        probation_end = start_date + relativedelta(months=3)
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                UPDATE employees SET hire_date = ?, start_date = ?,
                    probation_end_date = ?, onboarding_state = 'registered',
                    status = 'В адаптации', is_active = 1
                WHERE max_user_id = ?
                """,
                (start_date.isoformat(), start_date.isoformat(), probation_end.isoformat(), user_id),
            )
            await db.commit()
        await set_state(user_id, None)
        employee = await get_employee(user_id)
        await self.timeline.create_jobs(employee)
        await self.adapter.send_hr_notice(
            f"Новый пользователь {employee['employee_id']} запустил программу адаптации "
            f"(площадка: {employee['site']}).",
            fallback_user_id=user_id,
        )

    async def _finish_self_intro(
        self,
        user_id: str,
        text: str,
        data: dict,
        attachments: tuple[object, ...] = (),
    ) -> None:
        employee = await get_employee(user_id)
        if not employee:
            await set_state(user_id, None)
            return
        if data.get("skipped"):
            await set_state(user_id, None)
            await self._send(
                user_id,
                self._scenario_message("D2_10_selfintro_done", employee),
                stage_key="d2_10_selfintro.done",
            )
            return
        if not text or not attachments:
            await self._send(
                user_id,
                "Пришли одним сообщением краткое описание и фотографию.",
                stage_key="d2_10_selfintro.waiting",
            )
            return
        sender = getattr(self.adapter, "send_self_intro", None)
        if sender:
            await sender(
                user_id,
                employee["employee_code"],
                _mask_text(text),
                attachments,
            )
        else:
            await self.adapter.send_hr_notice(
                f"Самопредставление сотрудника {employee['employee_id']}: {_mask_text(text)}",
                fallback_user_id=user_id,
            )
        await set_state(user_id, None)
        await self._send(
            user_id,
            self._scenario_message("D2_10_selfintro_done", employee),
            stage_key="d2_10_selfintro.done",
        )

    async def _check_binding_code(self, user_id: str, code: str) -> None:
        employee = await get_employee_by_code(code)
        if not employee or (
            not str(employee["max_user_id"]).startswith("pending:")
            and str(employee["max_user_id"]) != user_id
        ):
            await self._send(
                user_id,
                self.messages["bind"]["wrong_code"],
                stage_key="registration.code.invalid",
            )
            return
        await set_state(user_id, "confirm_employee", {"employee_id": employee["id"]})
        await self._send(
            user_id,
            self.messages["bind"]["confirm"].format_map(
                _SafeFormat({"full_name": employee["full_name"]})
            ),
            [[("Подтверждаю", "reg:confirm"), ("Изменить", "reg:change")]],
            stage_key="registration.confirm",
            message_type="Информ+диалог",
        )

    async def _registration_callback(self, user_id: str, payload: str) -> None:
        state, data = await get_state(user_id)
        if payload == "reg:change":
            await set_state(user_id, "awaiting_code", {})
            await self._send(user_id, self.messages["bind"]["ask_code"], stage_key="registration.code")
            return
        if payload == "reg:confirm" and state == "confirm_employee":
            await link_employee(int(data["employee_id"]), user_id)
            today = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
            start_date = today + timedelta(days=3)
            probation_end = start_date + relativedelta(months=3)
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    """
                    UPDATE employees SET hire_date = ?, start_date = ?,
                        probation_end_date = ?, status = 'preboarding', is_active = 1
                    WHERE max_user_id = ?
                    """,
                    (start_date.isoformat(), start_date.isoformat(), probation_end.isoformat(), user_id),
                )
                await db.commit()
            await self._begin_pre_block(user_id)
            return
        await self._send(user_id, self._message("commands.stale_button"))

    async def _handoff(
        self,
        user_id: str,
        reason: str,
        source: str,
        context: str,
        *,
        notify_employee: bool = True,
    ) -> None:
        employee = await get_employee(user_id)
        if not employee:
            await set_state(user_id, "awaiting_code", {})
            await self._send(user_id, self._message("registration.ask_code"))
            return
        if employee["handoff_active"]:
            if notify_employee:
                await self._send(
                    user_id,
                    self._message("help.relay_done", employee, sla_hours=settings.HR_SLA_HOURS),
                )
            return
        safe_reason = _mask_text(reason)
        safe_context = _mask_text(context)
        ticket_id = await create_ticket(employee["id"], source, safe_reason, safe_context)
        await self.adapter.send_hr_notice(
            f"Тикет #{ticket_id} ({employee['employee_code']}, {employee['site']}): "
            f"{safe_reason}. Контекст: {safe_context}",
            fallback_user_id=user_id,
        )
        if notify_employee:
            await self._send(
                user_id,
                self._message("help.relay_done", employee, sla_hours=settings.HR_SLA_HOURS),
                stage_key="handoff.started",
            )

    async def _offer_free_question(
        self, user_id: str, employee: dict, text: str
    ) -> None:
        await set_state(
            user_id,
            "free_question_offer",
            {"question": _mask_text(text)},
        )
        await self._send(
            user_id,
            self._message("free_question", employee),
            self._help_buttons("free_question"),
            stage_key="free_question",
            message_type="Диалог",
        )

    async def _finish_help_relay(
        self, user_id: str, text: str, data: dict[str, Any]
    ) -> None:
        employee = await get_employee(user_id)
        if not employee:
            await set_state(user_id, None)
            await self._send(user_id, self._message("registration.ask_code"))
            return
        if not text:
            await self._send(user_id, self._message("help.expect_text_hint", employee))
            return
        context = data.get("original_question", "")
        if context:
            context = f"Вопрос сотрудника: {context}\nДополнение: {_mask_text(text)}"
        else:
            context = _mask_text(text)
        await self._handoff(
            user_id,
            _mask_text(text),
            f"Кнопка help:relay:{data.get('context', 'general')}",
            context,
        )
        if data.get("resume_state"):
            await set_state(
                user_id,
                "handoff_resume",
                {
                    "resume_state": data["resume_state"],
                    "resume_data": data["resume_data"],
                },
            )
        else:
            await set_state(user_id, None)

    async def _close_handoff(self, user_id: str, text: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда доступна только пользователям из ADMIN_USER_IDS.")
            return
        parts = text.split()
        employee = (
            await get_employee_by_code(parts[1])
            if len(parts) > 1
            else await get_employee(user_id)
        )
        if not employee:
            await self._send(user_id, "Не найден сотрудник. Формат: /close <employee_code>")
            return
        if await close_open_ticket(employee["id"]):
            await self._send(user_id, f"Тикет сотрудника {employee['employee_code']} закрыт.")
            if not str(employee["max_user_id"]).startswith("pending:"):
                target_user_id = str(employee["max_user_id"])
                await self._send(
                    target_user_id,
                    self._message("commands.handoff_closed", employee),
                    self._menu(),
                )
                state, state_data = await get_state(target_user_id)
                if state == "handoff_resume":
                    resume_state = state_data["resume_state"]
                    resume_data = state_data["resume_data"]
                    await set_state(target_user_id, resume_state, resume_data)
                    if resume_state == "survey":
                        await self._send_survey_question(target_user_id)
                    elif resume_state == "survey_reason":
                        await self._send(
                            target_user_id,
                            self._message("help.reason_prompt", employee),
                            [[("Пропустить", "reason:skip")]],
                        )
                    elif resume_state == "survey_text":
                        question = self.survey_definitions[resume_data["point"]]["questions"][
                            resume_data["question_index"]
                        ]
                        rows = (
                            [[("Пропустить", f"survey:open-skip:{resume_data['session_id']}:{resume_data['question_index']}")]]
                            if resume_data.get("text_mode") == "open"
                            else None
                        )
                        await self._send(
                            target_user_id,
                            self._survey_question_text(question),
                            rows,
                            message_type="Опрос",
                        )
        else:
            await self._send(user_id, "Открытое обращение не найдено.")

    async def _answer_free_text(self, user_id: str, employee: dict, text: str) -> None:
        normalized = text.casefold()
        sensitive = (
            "зарплат", "отпуск", "больнич", "увольн", "трудов", "конфликт",
            "здоров", "дисциплинар", "суд", "жалоб",
        )
        if any(term in normalized for term in sensitive):
            await self._handoff(
                user_id,
                "Чувствительная кадровая/юридическая тема",
                "Чувствительная тема",
                text,
            )
            await self._log_ai_request(employee, text, "Передан HR", "Чувствительная тема", 0.0, True)
            return
        result = await self._rag_stub(text, employee)
        if result is None:
            await self._handoff(user_id, "В базе знаний нет подтверждённого ответа", "RAG", text)
            await self._log_ai_request(employee, text, "Передан HR", "Недостаточно уверенности", 0.0, True)
            return
        answer, source, guardrail_blocked = result
        if guardrail_blocked:
            await self._handoff(user_id, "Ответ не прошёл проверку источника", "RAG", text)
            await self._log_ai_request(
                employee,
                text,
                "Передан HR",
                "Проверка источника не пройдена",
                0.0,
                True,
                source,
                guardrail_blocked=True,
            )
            return
        await self._send(
            user_id,
            f"{answer}\nИсточник: {source}",
            [[("👍", "feedback:positive"), ("👎", "feedback:negative")]],
            stage_key="ai.answer",
            message_type="ИИ-модуль",
        )
        await self._log_ai_request(
            employee, text, "Закрыт ИИ", None, 0.0, False, source
        )

    async def _rag_stub(
        self, text: str, employee: dict
    ) -> tuple[str, str, bool] | None:
        if not settings.ENABLE_RAG:
            return None
        result = await self.rag_engine.answer_question(
            text,
            plant_site=employee.get("site") or employee.get("plant_site") or "",
        )
        if (
            result["needs_escalation"]
            or result["confidence"] < settings.RAG_MIN_CONFIDENCE
            or not result["sources"]
        ):
            return None
        answer = result["answer"]
        source = result["sources"][0]
        source_title = source.split(" (стр.")[0]
        if source_title not in answer:
            return "", source, True
        return answer, source, False

    async def _log_ai_request(
        self,
        employee: dict,
        question: str,
        resolution: str,
        reason: str | None,
        confidence: float,
        knowledge_gap: bool,
        source_doc: str | None = None,
        guardrail_blocked: bool = False,
    ) -> None:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO ai_requests
                    (employee_id, topic, question_text, retrieval_confidence,
                     source_doc, resolution, resolved_by_ai, escalation_reason,
                     is_knowledge_gap, guardrail_blocked)
                VALUES (?, 'Другое', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    employee["id"],
                    _mask_text(question),
                    confidence,
                    source_doc,
                    resolution,
                    int(resolution == "Закрыт ИИ"),
                    reason,
                    int(knowledge_gap),
                    int(guardrail_blocked),
                ),
            )
            await db.commit()

    async def _start_survey(self, user_id: str, employee: dict, point: str) -> None:
        definition = self.survey_definitions[point]
        state, data = await get_state(user_id)
        if state in {"survey", "survey_text", "survey_reason"} and data.get("point") == point:
            return
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                """
                INSERT INTO survey_sessions (employee_id, survey_point)
                VALUES (?, ?)
                """,
                (employee["id"], definition["point"]),
            )
            session_id = int(cursor.lastrowid)
            await db.commit()
        await set_state(
            user_id,
            "survey",
            {"session_id": session_id, "point": point, "question_index": 0, "multi": []},
        )
        await self._send_survey_question(user_id)

    def _survey_question_text(self, question: dict[str, Any]) -> str:
        text_id = question.get("text_id")
        return self._scenario_message(text_id) if text_id else question["text"]

    async def _send_survey_question(self, user_id: str) -> None:
        state, data = await get_state(user_id)
        if state != "survey":
            return
        questions = self.survey_definitions[data["point"]]["questions"]
        index = data["question_index"]
        if index >= len(questions):
            await self._complete_survey(user_id, data)
            return
        question = questions[index]
        employee = await get_employee(user_id)
        question_text = self._survey_question_text(question)
        rows: list[list[tuple[str, str]]] = []
        if question["type"] == "yesno":
            options = ["Да", "Нет"]
        elif question["type"] == "scale10":
            options = [str(value) for value in range(1, 11)]
        elif question["type"] == "single":
            options = question["options"]
        elif question["type"] == "multi":
            options = question["options"]
            if employee:
                excluded = question.get("exclude_options", {}).get(employee["site"], [])
                options = [option for option in options if option not in excluded]
            selected = set(data.get("multi", []))
            for option_index, option in enumerate(options):
                marked = f"✅ {option}" if option_index in selected else option
                rows.append(
                    [(marked, f"survey:multi:{data['session_id']}:{index}:{option_index}")]
                )
            rows.append([("Готово", f"survey:multi-done:{data['session_id']}:{index}")])
        elif question["type"] == "open":
            await set_state(
                user_id,
                "survey_text",
                data | {"question_id": question["id"], "text_mode": "open"},
            )
            await self._send(
                user_id,
                question_text,
                [[("Пропустить", f"survey:open-skip:{data['session_id']}:{index}")]],
                stage_key=f"survey.{data['point']}",
                message_type="Опрос",
            )
            return
        else:
            raise ValueError(f"Unsupported survey question type: {question['type']}")
        if question["type"] == "scale10":
            for start in (0, 5):
                rows.append(
                    [
                        (
                            option,
                            f"survey:answer:{data['session_id']}:{index}:{start + offset}",
                        )
                        for offset, option in enumerate(options[start : start + 5])
                    ]
                )
        elif question["type"] != "multi":
            for start in range(0, len(options), 3):
                rows.append(
                    [
                        (
                            option,
                            f"survey:answer:{data['session_id']}:{index}:{start + offset}",
                        )
                        for offset, option in enumerate(options[start : start + 3])
                    ]
                )
        await self._send(
            user_id,
            question_text,
            rows,
            stage_key=f"survey.{data['point']}",
            message_type="Опрос",
        )

    async def _survey_callback(self, user_id: str, payload: str) -> None:
        parts = payload.split(":")
        state, data = await get_state(user_id)
        if len(parts) >= 3 and parts[1] == "resume":
            if (
                state in {"survey", "survey_text", "survey_reason"}
                and int(data.get("session_id", -1)) == int(parts[2])
            ):
                if state == "survey_text":
                    question = self.survey_definitions[data["point"]]["questions"][
                        data["question_index"]
                    ]
                    text = self._survey_question_text(question)
                    rows = (
                        [[("Пропустить", f"survey:open-skip:{data['session_id']}:{data['question_index']}")]]
                        if data.get("text_mode") == "open"
                        else None
                    )
                    await self._send(user_id, text, rows, message_type="Опрос")
                elif state == "survey_reason":
                    await self._send(
                        user_id,
                        self._message("help.reason_prompt", await get_employee(user_id)),
                        [[("Пропустить", "reason:skip")]],
                    )
                else:
                    await self._send_survey_question(user_id)
            else:
                await self._send(user_id, self._message("commands.stale_button"))
            return
        if len(parts) < 4 or int(data.get("session_id", -1)) != int(parts[2]):
            await self._send(user_id, self._message("commands.stale_button"))
            return
        if parts[1] == "open-skip" and state == "survey_text":
            await set_state(user_id, "survey", data | {"question_index": data["question_index"]})
            await self._save_open_survey_answer(user_id, "", data)
            return
        if state != "survey":
            await self._send(user_id, self._message("commands.stale_button"))
            return
        index = int(data["question_index"])
        questions = self.survey_definitions[data["point"]]["questions"]
        if index >= len(questions) or int(parts[3]) != index:
            await self._send(user_id, self._message("commands.stale_button"))
            return
        question = questions[index]
        if parts[1] == "multi":
            option_index = int(parts[4])
            options = await self._survey_options(user_id, question)
            if option_index >= len(options):
                await self._send(user_id, self._message("commands.stale_button"))
                return
            selected = set(data.get("multi", []))
            if option_index in selected:
                selected.remove(option_index)
            else:
                selected.add(option_index)
            data["multi"] = sorted(selected)
            await set_state(user_id, state, data)
            await self._send_survey_question(user_id)
            return
        if parts[1] == "multi-done":
            chosen = data.get("multi", [])
            options = await self._survey_options(user_id, question)
            answer = [options[i] for i in chosen]
            if question.get("other_option") in answer:
                await set_state(
                    user_id,
                    "survey_text",
                    data | {"text_mode": "other", "pending_answer": answer},
                )
                await self._send(user_id, "Напиши свой вариант одним сообщением.")
                return
            await self._save_answer(user_id, data, question, answer)
            return
        if parts[1] != "answer":
            return
        options = (
            ["Да", "Нет"]
            if question["type"] == "yesno"
            else [str(value) for value in range(1, 11)]
            if question["type"] == "scale10"
            else await self._survey_options(user_id, question)
        )
        option_index = int(parts[4])
        if option_index >= len(options):
            await self._send(user_id, self._message("commands.stale_button"))
            return
        answer: str = options[option_index]
        score, low = self._score(question, answer, option_index)
        if question.get("other_option") == answer:
            await set_state(
                user_id,
                "survey_text",
                data | {"text_mode": "other", "pending_answer": answer},
            )
            await self._send(user_id, "Напиши свой вариант одним сообщением.")
            return
        await self._save_answer(user_id, data, question, answer, score, low)

    async def _survey_options(
        self, user_id: str, question: dict[str, Any]
    ) -> list[str]:
        options = list(question.get("options", []))
        if question["type"] == "multi":
            employee = await get_employee(user_id)
            if employee:
                excluded = question.get("exclude_options", {}).get(employee["site"], [])
                options = [option for option in options if option not in excluded]
        return options

    @staticmethod
    def _score(
        question: dict[str, Any], answer: str, index: int
    ) -> tuple[float | None, bool]:
        if question["type"] == "scale10":
            value = float(answer)
            return (value - 1) / 9 * 100, value < 8
        if question["type"] == "yesno":
            return (100.0, False) if answer == "Да" else (0.0, True)
        return None, False

    async def _save_answer(
        self,
        user_id: str,
        data: dict,
        question: dict,
        answer: Any,
        score: float | None = None,
        low: bool = False,
        comment: str | None = None,
    ) -> None:
        answer_text = json.dumps(answer, ensure_ascii=False) if isinstance(answer, list) else str(answer)
        if score is None and question["type"] in {"yesno", "scale10"}:
            options = ["Да", "Нет"] if question["type"] == "yesno" else [str(v) for v in range(1, 11)]
            score, low = self._score(question, answer_text, options.index(answer_text))
        score_10 = (
            float(answer_text)
            if question["type"] == "scale10" and answer_text.isdigit()
            else None
        )
        employee = await get_employee(user_id)
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO survey_answers
                    (session_id, question_id, question_text, dimension, answer_type,
                     answer_value, score_10, score_pct, is_low, comment_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["session_id"],
                    question["id"],
                    self._survey_question_text(question),
                    question.get("dimension"),
                    question["type"],
                    answer_text,
                    score_10,
                    score,
                    int(low),
                    _mask_text(comment) if comment else None,
                ),
            )
            await db.commit()
        if comment:
            await self._analyze_text(user_id, data, comment, question["id"])
        if question["type"] == "scale10" and answer_text.isdigit() and int(answer_text) <= 3:
            await self._create_red_flag(
                user_id, data, "Низкая оценка", "Средний", answer_text
            )
        if data["point"] == "2months" and question["id"] == "mood" and answer_text.startswith("Уже устал"):
            await self._create_red_flag(user_id, data, "Выгорание / перегрузка", "Высокий", answer_text)
        followups = question.get("followups", {})
        if question["type"] == "multi" and isinstance(answer, list):
            selected_options = await self._survey_options(user_id, question)
            matched_followups = [
                followups.get(selected_options.index(item), {})
                for item in answer
                if item in selected_options
            ]
            option_index = -1
        else:
            option_index = (
                question.get("options", []).index(answer)
                if isinstance(answer, str) and answer in question.get("options", [])
                else -1
            )
            matched_followups = [
                followups.get(option_index, followups.get(str(option_index), {}))
            ]
        followup = next((item for item in matched_followups if item), {})
        if data["point"] == "end" and question["id"] == "stay" and option_index in {1, 2}:
            risk = "Высокий" if option_index == 2 else "Средний"
            await self._create_red_flag(
                user_id, data, "Намерение уйти", risk, answer_text
            )
            if option_index == 2:
                await self.adapter.send_head_hr_notice(
                    f"Высокий красный флаг: сотрудник {employee['employee_code'] if employee else ''} "
                    "задумывается об уходе по итогам испытательного срока.",
                    fallback_user_id=user_id,
                )
        for matched_followup in matched_followups:
            if not matched_followup.get("text_id"):
                continue
            followup = matched_followup
            if followup.get("wait_for_reason"):
                await set_state(
                    user_id,
                    "survey_reason",
                    data | {"pending_question": question, "reason_mode": "continue"},
                )
                await self._send(
                    user_id,
                    self._scenario_message(followup["text_id"], employee),
                    [[("Готов озвучить причину в личной коммуникации с рекрутером", "survey:reason:recruiter")]],
                    stage_key=f"survey.{data['point']}.reason",
                    message_type="Опрос",
                )
                return
            rows = (
                self._help_buttons(
                    f"survey_{data['point']}_{question['id']}",
                    relay=followup.get("help") == "both",
                )
                if followup.get("help")
                else None
            )
            await self._send(
                user_id,
                self._scenario_message(followup["text_id"], employee),
                rows,
                stage_key=f"survey.{data['point']}.{question['id']}.followup",
                message_type="Опрос",
            )
        if question["type"] in {"yesno", "scale10"} and low:
            await set_state(
                user_id,
                "survey_reason",
                data | {"pending_question": question, "reason_mode": "comment"},
            )
            await self._send(
                user_id,
                self._message("help.reason_prompt", employee),
                [[("Пропустить", "reason:skip")]],
                stage_key=f"survey.{data['point']}.reason",
                message_type="Опрос",
            )
            return
        await self._advance_survey(user_id, data)

    async def _save_survey_text(self, user_id: str, text: str, data: dict) -> None:
        questions = self.survey_definitions[data["point"]]["questions"]
        question = questions[data["question_index"]]
        if data.get("text_mode") == "other":
            answer = data["pending_answer"]
            if text:
                answer = [*(answer if isinstance(answer, list) else [answer]), _mask_text(text)]
            await self._save_answer(user_id, data, question, answer)
            return
        await self._save_answer(user_id, data, question, _mask_text(text), comment=text)

    async def _save_survey_reason(self, user_id: str, text: str, data: dict) -> None:
        question = data["pending_question"]
        if text:
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    """
                    UPDATE survey_answers SET comment_text = ?
                    WHERE session_id = ? AND question_id = ?
                    """,
                    (_mask_text(text), data["session_id"], question["id"]),
                )
                await db.commit()
            await self._analyze_text(user_id, data, text, question["id"])
        await self._advance_survey(user_id, data)

    async def _save_open_survey_answer(
        self, user_id: str, text: str, data: dict[str, Any]
    ) -> None:
        question = self.survey_definitions[data["point"]]["questions"][
            data["question_index"]
        ]
        await self._save_answer(
            user_id,
            data,
            question,
            _mask_text(text) if text else "",
            comment=text or None,
        )

    async def _advance_survey(self, user_id: str, data: dict[str, Any]) -> None:
        data.pop("pending_question", None)
        data.pop("reason_mode", None)
        data.pop("text_mode", None)
        data.pop("pending_answer", None)
        data["question_index"] += 1
        data["multi"] = []
        await set_state(user_id, "survey", data)
        await self._send_survey_question(user_id)

    async def _complete_survey(self, user_id: str, data: dict) -> None:
        if data["point"] == "end":
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    "UPDATE survey_sessions SET status = 'awaiting_goodbye' WHERE id = ?",
                    (data["session_id"],),
                )
                await db.commit()
            employee = await get_employee(user_id)
            await self._send(
                user_id,
                self._scenario_message("F_thanks", employee),
                stage_key="survey.end.completed",
                message_type="Опрос",
            )
            await self._send_playlist(
                user_id, self._scenario_message("F_playlist", employee)
            )
            await set_state(user_id, "survey_bye", data)
            await self._send(
                user_id,
                self._scenario_message("F_bye", employee),
                [[("Пока, Корсик! 💙", "bye:1"), ("😭😞😭😞🥺😞", "bye:2")]],
                stage_key="survey.end.goodbye",
                message_type="Опрос",
            )
            await self._send_asset(user_id, "img_16.png")
            return
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                UPDATE survey_sessions SET completed_at = CURRENT_TIMESTAMP,
                    status = 'completed'
                WHERE id = ?
                """,
                (data["session_id"],),
            )
            await db.commit()
        await set_state(user_id, None)
        definition = self.survey_definitions[data["point"]]
        completion_id = definition.get("completion_text_id")
        employee = await get_employee(user_id)
        message = (
            self._scenario_message(completion_id, employee)
            if completion_id
            else self._message("help.reason_thanks", employee)
        )
        await self._send(
            user_id,
            message,
            stage_key=f"survey.{data['point']}.completed",
            message_type="Опрос",
        )

    async def _finish_final_survey(self, user_id: str, payload: str) -> None:
        state, data = await get_state(user_id)
        if state != "survey_bye":
            return
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                UPDATE survey_sessions SET completed_at = CURRENT_TIMESTAMP,
                    status = 'completed'
                WHERE id = ?
                """,
                (data["session_id"],),
            )
            await db.execute(
                """
                UPDATE employees SET status = 'completed', onboarding_state = 'completed'
                WHERE max_user_id = ?
                """,
                (user_id,),
            )
            await db.execute(
                """
                UPDATE timeline_jobs SET status = 'cancelled'
                WHERE employee_id = (
                    SELECT id FROM employees WHERE max_user_id = ?
                ) AND status IN ('pending', 'waiting_approval')
                """,
                (user_id,),
            )
            await db.commit()
        await set_state(user_id, None)
        employee = await get_employee(user_id)
        await self._send(
            user_id,
            self._scenario_message("F_bye_reply", employee),
            stage_key="survey.end.goodbye.reply",
            message_type="Опрос",
        )

    async def _analyze_text(self, user_id: str, data: dict, text: str, question_id: str) -> None:
        lowered = text.casefold()
        flag_type = None
        risk = "Средний"
        high_terms = ("травм", "несчастн", "домога", "дискриминац")
        medium_terms = (
            "увольн", "ухожу", "уйду", "буллинг", "оскорб", "унижа",
            "давлен", "выгора", "тревог", "депресс",
        )
        if any(term in lowered for term in high_terms):
            flag_type, risk = "Угроза безопасности / дискриминация", "Высокий"
        elif any(term in lowered for term in medium_terms):
            flag_type, risk = "Риск адаптации", "Средний"
        if not flag_type:
            return
        await self._create_red_flag(user_id, data, flag_type, risk, text)

    async def _create_red_flag(
        self, user_id: str, data: dict, flag_type: str, risk: str, quote: str
    ) -> None:
        employee = await get_employee(user_id)
        if not employee:
            return
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                """
                INSERT INTO red_flags
                    (employee_id, survey_session_id, source_point, flag_type,
                     risk_level, quote, alert_hr_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    employee["id"],
                    data.get("session_id"),
                    data.get("point"),
                    flag_type,
                    risk,
                    _mask_text(quote),
                ),
            )
            flag_id = cursor.lastrowid
            await db.commit()
        await self.adapter.send_hr_notice(
            f"Красный флаг #{flag_id}: {employee['employee_code']}, {employee['site']}, "
            f"{flag_type} ({risk}). Цитата: {_mask_text(quote)}",
            fallback_user_id=str(employee["max_user_id"]),
        )
        if employee.get("manager_id") and flag_type != "Токсичность в коллективе":
            await self.adapter.send_hr_notice(
                f"[ЗАГЛУШКА push руководителю {employee['manager_id']}] "
                f"Провести встречу с {employee['employee_code']}.",
                fallback_user_id=str(employee["max_user_id"]),
            )
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    """
                    INSERT INTO manager_actions
                        (employee_id, manager_id, action_type, sent_at, status)
                    VALUES (?, ?, 'Встреча по красному флагу', CURRENT_TIMESTAMP, 'sent')
                    """,
                    (employee["id"], employee["manager_id"]),
                )
                await db.commit()

    async def _add_manager_action(self, employee: dict) -> None:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO manager_actions
                    (employee_id, manager_id, action_type, sent_at, due_at)
                VALUES (?, ?, 'Плановая встреча по адаптации',
                        CURRENT_TIMESTAMP, datetime('now', '+3 days'))
                """,
                (employee["id"], employee.get("manager_id")),
            )
            await db.commit()
        await self.adapter.send_hr_notice(
            f"[ЗАГЛУШКА push руководителю {employee.get('manager_id')}] "
            f"Провести встречу по адаптации: {employee['employee_code']}.",
            fallback_user_id=str(employee["max_user_id"]),
        )

    async def _schedule_transport_reminder(self, employee: dict | None) -> None:
        if not employee:
            return
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM timeline_jobs
                WHERE employee_id = ? AND stage_key LIKE 'transport_reminder_%'
                """,
                (employee["id"],),
            )
            count = (await cursor.fetchone())[0]
            if count >= 2:
                return
            planned = datetime.now(ZoneInfo(settings.TIMEZONE)) + timedelta(hours=1)
            await db.execute(
                """
                INSERT OR IGNORE INTO timeline_jobs
                    (employee_id, stage_key, stage_no, stage_name, message_type, planned_at)
                VALUES (?, ?, 1, 'Напоминание о транспорте', 'Отложенное уведомление', ?)
                """,
                (employee["id"], f"transport_reminder_{count + 1}", planned.isoformat()),
            )
            await db.commit()

    async def run_survey_reminders(self) -> int:
        now = datetime.now(ZoneInfo(settings.TIMEZONE))
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.id, s.employee_id, s.started_at, s.reminders_sent,
                       e.max_user_id, e.address_form, e.handoff_active, e.is_active
                FROM survey_sessions s JOIN employees e ON e.id = s.employee_id
                WHERE s.status = 'in_progress'
                """
            )
            sessions = await cursor.fetchall()
        sent = 0
        for row in sessions:
            session = dict(row)
            if not session["is_active"] or session["handoff_active"]:
                continue
            started = datetime.fromisoformat(session["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=ZoneInfo(settings.TIMEZONE))
            age = now - started
            reminder_scale = max(float(settings.TIME_SCALE), 0.000001)
            if (
                session["reminders_sent"] < 2
                and age >= timedelta(days=(session["reminders_sent"] + 1) * reminder_scale)
            ):
                employee = await get_employee(str(session["max_user_id"]))
                await self._send(
                    str(session["max_user_id"]),
                    self._scenario_message("survey_reminder", employee),
                    [[("▶️ Пройти опрос", f"survey:resume:{session['id']}")]],
                    stage_key="survey.reminder",
                    message_type="Отложенное уведомление",
                )
                await self._send_asset(str(session["max_user_id"]), "img_12.png")
                async with aiosqlite.connect(_db_path()) as db:
                    await db.execute(
                        "UPDATE survey_sessions SET reminders_sent = reminders_sent + 1 WHERE id = ?",
                        (session["id"],),
                    )
                    await db.commit()
                sent += 1
        return sent

    async def run_hr_sla_reminders(self) -> int:
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT t.id, e.employee_code
                FROM tickets t JOIN employees e ON e.id = t.employee_id
                WHERE t.status = 'open' AND t.first_response_at IS NULL
                  AND t.sla_reminded_at IS NULL
                  AND t.created_at <= datetime('now', ?)
                """,
                (f"-{int(settings.HR_SLA_HOURS)} hours",),
            )
            tickets = await cursor.fetchall()
        for ticket in tickets:
            await self.adapter.send_hr_notice(
                f"Напоминание по SLA: тикет #{ticket['id']} для {ticket['employee_code']} "
                f"ожидает ответа более {settings.HR_SLA_HOURS} ч."
            )
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    "UPDATE tickets SET sla_reminded_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (ticket["id"],),
                )
                await db.commit()
        return len(tickets)

    async def send_weekly_digest(self) -> None:
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT e.site, COUNT(*) AS flags
                FROM red_flags r JOIN employees e ON e.id = r.employee_id
                WHERE r.detected_at >= datetime('now', '-7 days')
                GROUP BY e.site ORDER BY flags DESC
                """
            )
            flags_by_site = await cursor.fetchall()
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM ai_requests
                WHERE is_knowledge_gap = 1
                  AND asked_at >= datetime('now', '-7 days')
                """
            )
            knowledge_gaps = (await cursor.fetchone())[0]
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM tickets
                WHERE created_at >= datetime('now', '-7 days')
                """
            )
            ticket_count = (await cursor.fetchone())[0]
        site_summary = (
            "\n".join(f"- {row['site']}: {row['flags']} красн. флагов" for row in flags_by_site)
            or "- Новых красных флагов нет"
        )
        await self.adapter.send_head_hr_notice(
            "Еженедельный дайджест Корсика (агрегированные данные за 7 дней)\n"
            f"Тикетов HR: {ticket_count}\n"
            f"Вопросов без ответа в базе знаний: {knowledge_gaps}\n"
            f"Красные флаги по площадкам:\n{site_summary}"
        )

    async def _simulate(self, user_id: str, text: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= len(STAGES):
            await self._send(user_id, f"Формат: /simulate <stage 1-{len(STAGES)}>")
            return
        stage_key = STAGES[int(parts[1]) - 1][0]
        await self._jump_stage(user_id, f"/jump {stage_key}")

    async def _approve_stage(self, user_id: str, text: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        parts = text.split()
        if len(parts) != 3 or parts[2] not in {"day2", "day3"}:
            await self._send(user_id, "Формат: /approve <employee_code> <day2|day3>")
            return
        employee = await get_employee_by_code(parts[1])
        if not employee:
            await self._send(user_id, "Сотрудник с таким кодом не найден.")
            return
        stage_key = "d2_10_selfintro" if parts[2] == "day2" else "d3_09_plan"
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO recruiter_approvals (employee_id, stage_key, approved_by)
                VALUES (?, ?, ?)
                ON CONFLICT(employee_id, stage_key) DO UPDATE SET
                    approved_by = excluded.approved_by,
                    approved_at = CURRENT_TIMESTAMP
                """,
                (employee["id"], stage_key, user_id),
            )
            await db.execute(
                """
                UPDATE timeline_jobs SET status = 'pending', planned_at = ?
                WHERE employee_id = ? AND stage_key = ?
                  AND status = 'waiting_approval'
                """,
                (
                    datetime.now(ZoneInfo(settings.TIMEZONE)).isoformat(),
                    employee["id"],
                    stage_key,
                ),
            )
            await db.commit()
        await self._send(
            user_id,
            f"Отмашка для {employee['employee_code']} ({parts[2]}) сохранена.",
        )

    async def _export_analytics(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        from monitor.analytics import export_analytics

        paths = await export_analytics()
        await self._send(user_id, f"Экспортировано файлов: {len(paths)}. Папка: exports/")

    async def _is_authorized_test_user(self, user_id: str) -> bool:
        admins = {
            item.strip()
            for item in settings.ADMIN_USER_IDS.split(",")
            if item.strip()
        }
        if admins:
            return user_id in admins
        if not settings.DEMO_MODE:
            return False
        return await get_employee(user_id) is not None

    async def _send_help(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Сначала привяжи профиль через /start.")
            return
        await self._send(
            user_id,
            "Команды Корсика:\n"
            "/stages — этапы и статусы\n"
            "/next — выполнить следующий этап\n"
            "/jump <ключ> — выполнить конкретный этап\n"
            "/me — профиль\n"
            "/stats — статистика\n"
            "/reset — сбросить демо-профиль\n"
            "/Food — информация об обеде\n"
            "/HR — передать вопрос в HR",
        )

    async def _show_stages(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Сначала привяжи профиль через /start.")
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Профиль не найден. Используй /start.")
            return
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT stage_key, stage_name, planned_at, status
                FROM timeline_jobs WHERE employee_id = ?
                ORDER BY stage_no
                """,
                (employee["id"],),
            )
            jobs = await cursor.fetchall()
        if not jobs:
            await self._send(user_id, "Этапы пока не созданы. Заверши привязку через /start.")
            return
        text = "\n".join(
            f"{row['stage_key']} — {row['status']} ({row['planned_at'][:16]})"
            for row in jobs
        )
        await self._send(user_id, text, message_type="Информ")

    async def _run_next_stage(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Сначала привяжи профиль через /start.")
            return
        if employee["handoff_active"]:
            await self._send(user_id, "Есть активное обращение в HR. Закрой его перед продолжением.")
            return
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT j.*, e.max_user_id, e.full_name, e.site, e.plant_site,
                       e.employee_category, e.is_office, e.address_form,
                       e.hire_date, e.handoff_active, e.is_active
                FROM timeline_jobs j JOIN employees e ON e.id = j.employee_id
                WHERE j.employee_id = ? AND j.status IN ('pending', 'waiting_approval')
                ORDER BY j.stage_no LIMIT 100
                """,
                (employee["id"],),
            )
            jobs = [dict(row) for row in await cursor.fetchall()]
        for job in jobs:
            if not is_audience_eligible(job["stage_key"], job):
                await self.timeline._set_status(job["id"], "skipped")
                continue
            if (
                job["stage_key"] in {"d2_10_selfintro", "d3_09_plan"}
                and not settings.DEMO_MODE
                and not await self.timeline._is_approved(job)
            ):
                await self.timeline._set_status(job["id"], "waiting_approval")
                await self._send(user_id, f"Этап {job['stage_key']} ожидает /approve.")
                return
            await self._dispatch_manual_stage(user_id, job)
            return
        await self._send(user_id, "Нет ожидающих этапов.")

    async def _jump_stage(self, user_id: str, command: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        parts = command.split()
        if len(parts) != 2 or parts[1] not in STAGE_HANDLERS:
            await self._send(
                user_id,
                "Формат: /jump <ключ>. Используй /stages для просмотра этапов.",
            )
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Сначала привяжи профиль через /start.")
            return
        stage_key = parts[1]
        if not is_audience_eligible(stage_key, employee):
            async with aiosqlite.connect(_db_path()) as db:
                await db.execute(
                    "UPDATE timeline_jobs SET status = 'skipped' WHERE employee_id = ? AND stage_key = ?",
                    (employee["id"], stage_key),
                )
                await db.commit()
            await self._send(user_id, f"Этап {stage_key} пропущен: не подходит аудитория.")
            return
        if stage_key == "pre_block":
            await self.send_stage(employee, stage_key)
            return
        stage = next((item for item in STAGES if item[0] == stage_key), None)
        if stage is None:
            await self._send(user_id, f"Неизвестный этап: {stage_key}.")
            return
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM timeline_jobs
                WHERE employee_id = ? AND stage_key = ?
                """,
                (employee["id"], stage_key),
            )
            job_row = await cursor.fetchone()
            if job_row is None:
                await db.execute(
                    """
                    INSERT INTO timeline_jobs
                        (employee_id, stage_key, stage_no, stage_name,
                         message_type, planned_at)
                    VALUES (?, ?, ?, ?, 'scenario', ?)
                    """,
                    (
                        employee["id"],
                        stage_key,
                        stage[1],
                        stage[2],
                        datetime.now(ZoneInfo(settings.TIMEZONE)).isoformat(),
                    ),
                )
                await db.commit()
                cursor = await db.execute(
                    "SELECT * FROM timeline_jobs WHERE employee_id = ? AND stage_key = ?",
                    (employee["id"], stage_key),
                )
                job_row = await cursor.fetchone()
        assert job_row is not None
        job = dict(job_row) | {
            "max_user_id": employee["max_user_id"],
            "full_name": employee["full_name"],
            "site": employee["site"],
            "plant_site": employee["plant_site"],
            "employee_category": employee["employee_category"],
            "is_office": employee["is_office"],
            "address_form": employee["address_form"],
            "hire_date": employee["hire_date"],
            "handoff_active": employee["handoff_active"],
            "is_active": employee["is_active"],
        }
        if job["status"] in {"sent", "skipped", "cancelled", "disabled"}:
            await self._send(user_id, f"Этап {stage_key} уже имеет статус {job['status']}.")
            return
        if (
            stage_key in {"d2_10_selfintro", "d3_09_plan"}
            and not settings.DEMO_MODE
            and not await self.timeline._is_approved(job)
        ):
            await self.timeline._set_status(job["id"], "waiting_approval")
            await self._send(user_id, f"Этап {stage_key} ожидает /approve.")
            return
        await self._dispatch_manual_stage(user_id, job)

    async def _dispatch_manual_stage(self, user_id: str, job: dict[str, Any]) -> None:
        await self.timeline._set_status(job["id"], "sent")
        try:
            await self.send_stage(job, job["stage_key"])
        except Exception:
            logger.exception(
                "Manual stage failed: employee=%s stage=%s",
                job["employee_id"],
                job["stage_key"],
            )
            await self.timeline._set_status(job["id"], "failed")
            await self._send(
                user_id,
                f"Не удалось выполнить этап {job['stage_key']}; он отмечен как failed.",
            )
            return
        await self._send(user_id, f"Выполнен этап {job['stage_key']}.")

    async def _show_profile(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Профиль не найден. Используй /start.")
            return
        office = "офис" if employee["is_office"] else "не офис"
        await self._send(
            user_id,
            f"{employee['full_name']} ({employee['employee_id']})\n"
            f"Площадка: {employee['site']}; категория: {office}\n"
            f"Дата выхода: {employee['start_date'] or 'не назначена'}\n"
            f"Статус: {employee['status']}; обращение HR: "
            f"{'активно' if employee['handoff_active'] else 'нет'}",
        )

    async def _show_stats(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Профиль не найден. Используй /start.")
            return
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM timeline_jobs WHERE employee_id = ? GROUP BY status",
                (employee["id"],),
            )
            stage_counts = {status: count for status, count in await cursor.fetchall()}
            cursor = await db.execute(
                "SELECT COUNT(*) FROM survey_sessions WHERE employee_id = ? AND status = 'completed'",
                (employee["id"],),
            )
            completed_surveys = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE employee_id = ?",
                (employee["id"],),
            )
            tickets = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT COUNT(*) FROM red_flags WHERE employee_id = ?",
                (employee["id"],),
            )
            flags = (await cursor.fetchone())[0]
        stage_summary = ", ".join(
            f"{status}: {count}" for status, count in sorted(stage_counts.items())
        ) or "этапов нет"
        await self._send(
            user_id,
            f"Этапы — {stage_summary}\n"
            f"Завершено опросов: {completed_surveys}; тикетов: {tickets}; "
            f"красных флагов: {flags}.",
        )

    async def _reset_employee(self, user_id: str) -> None:
        if not await self._is_authorized_test_user(user_id):
            await self._send(user_id, "Команда недоступна: проверь DEMO_MODE и ADMIN_USER_IDS.")
            return
        employee = await get_employee(user_id)
        if not employee:
            await self._send(user_id, "Профиль не найден. Используй /start.")
            return
        employee_id = employee["id"]
        pending_user_id = f"pending:{employee['employee_id']}"
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                "DELETE FROM survey_answers WHERE session_id IN (SELECT id FROM survey_sessions WHERE employee_id = ?)",
                (employee_id,),
            )
            for table in (
                "timeline_jobs",
                "survey_sessions",
                "tickets",
                "red_flags",
                "manager_actions",
                "ai_requests",
                "recruiter_approvals",
                "sent_triggers",
            ):
                await db.execute(f"DELETE FROM {table} WHERE employee_id = ?", (employee_id,))
            await db.execute(
                "DELETE FROM interactions WHERE max_user_id = ?",
                (user_id,),
            )
            await db.execute("DELETE FROM messages_log WHERE employee_id = ?", (employee_id,))
            await db.execute(
                "DELETE FROM conversation_states WHERE max_user_id = ?",
                (user_id,),
            )
            await db.execute(
                """
                UPDATE employees SET max_user_id = ?, onboarding_state = 'unregistered',
                    status = 'unregistered', is_active = 1, handoff_active = 0,
                    address_form = 'ty', start_date = NULL, probation_end_date = NULL,
                    termination_date = NULL, risk_status = 'normal'
                WHERE id = ?
                """,
                (pending_user_id, employee_id),
            )
            await db.commit()
        await self._send(user_id, "Профиль сброшен. Начни повторную привязку командой /start.")

    @staticmethod
    def _document_list(site: str | None) -> str:
        documents = list(DOCUMENTS)
        if site == "GTP":
            documents.append("Фотографии 3×4, 2 шт. (цветные или чёрно-белые)")
        return "\n".join(f"{i}. {item}" for i, item in enumerate(documents, 1))

    @staticmethod
    def _menu() -> list[list[tuple[str, str]]]:
        return [
            [("О боте", "menu:about"), ("Помощь", "menu:help")],
            [("Где пообедать", "food:yes"), ("Связаться с рекрутером", "hr:contact")],
        ]
