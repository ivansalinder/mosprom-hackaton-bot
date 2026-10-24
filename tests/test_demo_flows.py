import tempfile
import unittest
from pathlib import Path

import aiosqlite

from monitor.analytics import export_analytics
from monitor.bot.max_adapter import IncomingCallback, IncomingMessage
from monitor.config import settings
from monitor.db import init_db
from monitor.flows import KorsikFlows
from monitor.storage import get_employee, get_state


class FakeAdapter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, list | None]] = []
        self.hr_notices: list[str] = []
        self.assets: list[tuple[str, str]] = []

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
        self.hr_notices.append(f"Самопредставление {employee_code}: {text}")


class DemoFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = settings.SQLITE_DB_PATH
        settings.SQLITE_DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        await init_db()
        self.adapter = FakeAdapter()
        self.flows = KorsikFlows(self.adapter)
        self.flows.pre_block_delay = 0
        self.user_id = "987654321"

    async def asyncTearDown(self) -> None:
        settings.SQLITE_DB_PATH = self.previous_db_path
        self.temp_dir.cleanup()

    async def _register(self) -> dict:
        await self.flows.start(IncomingMessage(self.user_id, "/start"))
        self.assertEqual((await get_state(self.user_id))[0], "awaiting_code")
        await self.flows.message(IncomingMessage(self.user_id, "ябЛОКО"))
        await self.flows.callback(
            IncomingCallback(self.user_id, "reg:confirm")
        )
        await self.flows.callback(
            IncomingCallback(self.user_id, "addr:ty")
        )
        await self.flows.callback(
            IncomingCallback(self.user_id, "transport:no")
        )
        employee = await get_employee(self.user_id)
        assert employee is not None
        return employee

    async def test_registration_creates_persistent_timeline(self) -> None:
        employee = await self._register()
        self.assertEqual(employee["onboarding_state"], "registered")
        self.assertEqual(employee["site"], "GrPP")
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM timeline_jobs WHERE employee_id = ?",
                (employee["id"],),
            )
            self.assertEqual((await cursor.fetchone())[0], 23)

    async def test_day_three_survey_persists_and_completes(self) -> None:
        employee = await self._register()
        await self.flows._start_survey(self.user_id, employee, "day3")
        for question_index in range(3):
            _, state_data = await get_state(self.user_id)
            await self.flows.callback(
                IncomingCallback(
                    self.user_id,
                    f"survey:answer:{state_data['session_id']}:{question_index}:0",
                )
            )
        self.assertIsNone((await get_state(self.user_id))[0])
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM survey_sessions WHERE employee_id = ?",
                (employee["id"],),
            )
            self.assertEqual((await cursor.fetchone())[0], "completed")

    async def test_hr_command_enters_handoff(self) -> None:
        employee = await self._register()
        await self.flows.message(IncomingMessage(self.user_id, "/HR"))
        updated = await get_employee(self.user_id)
        self.assertEqual(updated["handoff_active"], 1)
        async with aiosqlite.connect(settings.resolved_sqlite_db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE employee_id = ? AND status = 'open'",
                (employee["id"],),
            )
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_export_creates_eight_pseudonymous_files(self) -> None:
        await self._register()
        output_dir = Path(self.temp_dir.name) / "exports"
        files = await export_analytics(output_dir)
        self.assertEqual(len(files), 8)
        self.assertTrue(all(path.exists() for path in files))
        exported = "\n".join(path.read_text(encoding="utf-8-sig") for path in files)
        self.assertIn("E-0001", exported)
        self.assertNotIn("Демо Сотрудник", exported)
        self.assertNotIn(self.user_id, exported)


if __name__ == "__main__":
    unittest.main()
