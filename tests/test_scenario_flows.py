import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from monitor.assets import asset_stub
from monitor.bot.max_adapter import IncomingCallback, IncomingMessage
from monitor.config import settings
from monitor.db import init_db
from monitor.flows import KorsikFlows
from monitor.stages import STAGE_HANDLERS, is_audience_eligible
from monitor.storage import get_employee, get_state
from monitor.timeline import STAGES


class FakeAdapter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, list | None]] = []
        self.assets: list[tuple[str, str]] = []
        self.hr_notices: list[str] = []

    async def send(self, user_id: str, text: str, rows: list | None = None) -> None:
        self.messages.append((user_id, text, rows))

    async def send_asset_or_stub(self, user_id: str, asset_name: str) -> None:
        self.assets.append((user_id, asset_name))

    async def send_hr_notice(
        self, text: str, fallback_user_id: str | None = None
    ) -> None:
        self.hr_notices.append(text)

    async def send_head_hr_notice(
        self, text: str, fallback_user_id: str | None = None
    ) -> None:
        self.hr_notices.append(text)

    async def send_link_button(
        self, user_id: str, text: str, label: str, url: str
    ) -> None:
        self.messages.append((user_id, text, [[(label, url)]]))

    async def send_self_intro(
        self,
        user_id: str,
        employee_code: str,
        text: str,
        attachments: tuple[object, ...],
    ) -> None:
        self.hr_notices.append(f"{employee_code}: {text}")


class ScenarioFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = settings.SQLITE_DB_PATH
        self.old_admin_ids = settings.ADMIN_USER_IDS
        self.old_demo_mode = settings.DEMO_MODE
        settings.SQLITE_DB_PATH = str(Path(self.temp_dir.name) / "scenario.db")
        settings.ADMIN_USER_IDS = ""
        settings.DEMO_MODE = True
        await init_db()
        self.adapter = FakeAdapter()
        self.flows = KorsikFlows(self.adapter)
        self.flows.pre_block_delay = 0

    async def asyncTearDown(self) -> None:
        settings.SQLITE_DB_PATH = self.old_db_path
        settings.ADMIN_USER_IDS = self.old_admin_ids
        settings.DEMO_MODE = self.old_demo_mode
        self.temp_dir.cleanup()

    async def register(self, user_id: str = "9001001", code: str = "яблоко") -> dict:
        await self.flows.start(IncomingMessage(user_id, "/start"))
        await self.flows.message(IncomingMessage(user_id, f"  {code.upper()}  "))
        await self.flows.callback(IncomingCallback(user_id, "reg:confirm"))
        await self.flows.callback(IncomingCallback(user_id, "addr:ty"))
        await self.flows.callback(IncomingCallback(user_id, "transport:no"))
        employee = await get_employee(user_id)
        assert employee is not None
        return employee

    async def test_binding_normalizes_code_and_rejects_unknown_code(self) -> None:
        user_id = "9001001"
        await self.flows.start(IncomingMessage(user_id, "/start"))
        await self.flows.message(IncomingMessage(user_id, "неизвестно"))
        self.assertEqual((await get_state(user_id))[0], "awaiting_code")
        await self.flows.message(IncomingMessage(user_id, "  ЯбЛоКО  "))
        self.assertEqual((await get_state(user_id))[0], "confirm_employee")

    async def test_address_form_falls_back_to_ty(self) -> None:
        employee = await self.register()
        employee["address_form"] = "vy"
        text = self.flows._format_message({"ty": "Привет, {name}"}, employee)
        self.assertEqual(text, f"Привет, {employee['full_name']}")

    async def test_document_lists_match_site_rules(self) -> None:
        self.assertEqual(len(self.flows._document_list("GTP").splitlines()), 9)
        self.assertEqual(len(self.flows._document_list("GrPP").splitlines()), 8)

    async def test_map_is_skipped_for_grpp_without_sending(self) -> None:
        employee = await self.register()
        self.adapter.messages.clear()
        await self.flows._run_scenario_stage("d1_11_map", employee)
        self.assertEqual(self.adapter.messages, [])
        self.assertEqual(
            await self._stage_status(employee["id"], "d1_11_map"), "skipped"
        )

    async def test_office_only_stage_is_skipped_for_non_office(self) -> None:
        employee = await self.register("9001002", "вишня")
        self.adapter.messages.clear()
        await self.flows._run_scenario_stage("d1_18_end", employee)
        self.assertEqual(self.adapter.messages, [])
        self.assertEqual(
            await self._stage_status(employee["id"], "d1_18_end"), "skipped"
        )

    async def test_access_no_creates_hr_ticket(self) -> None:
        employee = await self.register()
        await self.flows.callback(IncomingCallback("9001001", "access:no"))
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE employee_id = ? AND status = 'open'",
                (employee["id"],),
            )
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_day_three_negative_answer_shows_scenario_branch(self) -> None:
        employee = await self.register()
        await self.flows._start_survey("9001001", employee, "day3")
        state, data = await get_state("9001001")
        await self.flows.callback(
            IncomingCallback(
                "9001001",
                f"survey:answer:{data['session_id']}:0:2",
            )
        )
        self.assertTrue(any("Очень важно иметь возможность" in text for _, text, _ in self.adapter.messages))
        self.assertTrue(
            any(
                rows and rows[0][0][1].startswith("help:relay:")
                for _, _, rows in self.adapter.messages
            )
        )

    async def test_low_rating_asks_reason_but_rating_nine_does_not(self) -> None:
        employee = await self.register()
        await self.flows._start_survey("9001001", employee, "2weeks")
        _, data = await get_state("9001001")
        await self.flows.callback(
            IncomingCallback("9001001", f"survey:answer:{data['session_id']}:0:1")
        )
        self.assertEqual((await get_state("9001001"))[0], "survey_reason")
        second_employee = await self.register("9001002", "груша")
        await self.flows._start_survey("9001002", second_employee, "2weeks")
        _, data = await get_state("9001002")
        await self.flows.callback(
            IncomingCallback("9001002", f"survey:answer:{data['session_id']}:0:0")
        )
        _, data = await get_state("9001002")
        await self.flows.callback(
            IncomingCallback("9001002", f"survey:answer:{data['session_id']}:1:0")
        )
        _, data = await get_state("9001002")
        await self.flows.callback(
            IncomingCallback("9001002", f"survey:answer:{data['session_id']}:2:8")
        )
        self.assertEqual((await get_state("9001002"))[0], "survey")

    async def test_final_survey_c_answer_creates_high_flag(self) -> None:
        await self.register()
        await self.flows._start_survey("9001001", await get_employee("9001001"), "end")
        _, data = await get_state("9001001")
        await self.flows.callback(
            IncomingCallback("9001001", f"survey:answer:{data['session_id']}:0:0")
        )
        _, data = await get_state("9001001")
        await self.flows.callback(
            IncomingCallback("9001001", f"survey:answer:{data['session_id']}:1:2")
        )
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT risk_level FROM red_flags WHERE source_point = 'end'"
            )
            self.assertEqual((await cursor.fetchone())[0], "Высокий")
        self.assertTrue(any("Высокий красный флаг" in text for text in self.adapter.hr_notices))

    async def test_next_runs_pending_stages_in_order_once(self) -> None:
        employee = await self.register()
        await self.flows.message(IncomingMessage("9001001", "/next"))
        await self.flows.message(IncomingMessage("9001001", "/next"))
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT stage_key, status FROM timeline_jobs WHERE employee_id = ? AND stage_no <= 2 ORDER BY stage_no",
                (employee["id"],),
            )
            rows = await cursor.fetchall()
        self.assertEqual(rows, [("d_minus_1_ready", "sent"), ("d_minus_1_docs", "sent")])
        ready_text = self.flows._scenario_message(
            "D1M_ready_q", await get_employee("9001001")
        )
        self.assertEqual(sum(text == ready_text for _, text, _ in self.adapter.messages), 1)

    async def test_reset_restores_binding_prompt(self) -> None:
        employee = await self.register()
        await self.flows.message(IncomingMessage("9001001", "/reset"))
        self.assertIsNone(await get_employee("9001001"))
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT max_user_id, onboarding_state FROM employees WHERE id = ?",
                (employee["id"],),
            )
            self.assertEqual((await cursor.fetchone()), (f"pending:{employee['employee_id']}", "unregistered"))
        await self.flows.start(IncomingMessage("9001001", "/start"))
        self.assertEqual((await get_state("9001001"))[0], "awaiting_code")

    async def test_jump_does_not_repeat_sent_stage(self) -> None:
        await self.register()
        await self.flows.message(IncomingMessage("9001001", "/jump d1_1201_postcard"))
        sent_count = len(self.adapter.messages)
        await self.flows.message(IncomingMessage("9001001", "/jump d1_1201_postcard"))
        self.assertEqual(len(self.adapter.messages), sent_count + 1)
        self.assertIn("уже имеет статус sent", self.adapter.messages[-1][1])
        self.assertIn(("9001001", "postcard_1.png"), self.adapter.assets)

    async def test_incoming_message_log_masks_personal_data(self) -> None:
        await self.register()
        await self.flows.message(
            IncomingMessage(
                "9001001",
                "Мой номер +7 999 123-45-67 и почта demo@example.com",
            )
        )
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT text FROM messages_log WHERE direction = 'in' ORDER BY id DESC LIMIT 1"
            )
            stored = (await cursor.fetchone())[0]
        self.assertNotIn("demo@example.com", stored)
        self.assertNotIn("999 123-45-67", stored)

    async def test_free_question_waits_for_explicit_relay(self) -> None:
        await self.register()
        await self.flows.message(IncomingMessage("9001001", "Как устроен отпуск?"))
        self.assertEqual((await get_state("9001001"))[0], "free_question_offer")
        self.assertTrue(self.adapter.messages[-1][2][0][0][1].startswith("help:relay:"))
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM tickets")
            self.assertEqual((await cursor.fetchone())[0], 0)
        await self.flows.callback(
            IncomingCallback("9001001", "help:relay:free_question")
        )
        await self.flows.message(IncomingMessage("9001001", "Нужна консультация."))
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM tickets")
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_missing_asset_stub_and_all_handlers(self) -> None:
        self.assertEqual(
            asset_stub("adaptation_plan.pdf"),
            "📎 [TODO: Адаптационный план]",
        )
        self.assertTrue({stage[0] for stage in STAGES}.issubset(STAGE_HANDLERS))
        self.assertIn("pre_block", STAGE_HANDLERS)
        result = subprocess.run(
            [sys.executable, "scripts/check_scenario_texts.py"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    async def test_audience_helper_covers_non_office(self) -> None:
        employee = await self.register("9001002", "вишня")
        self.assertFalse(is_audience_eligible("d1_18_end", employee))

    async def _stage_status(self, employee_id: int, stage_key: str) -> str | None:
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM timeline_jobs WHERE employee_id = ? AND stage_key = ?",
                (employee_id, stage_key),
            )
            row = await cursor.fetchone()
            return row[0] if row else None


if __name__ == "__main__":
    unittest.main()
