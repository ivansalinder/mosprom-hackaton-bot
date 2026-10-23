import json
from datetime import date
import aiosqlite
from monitor.config import settings
from monitor.bot.delivery import MaxMessengerClient
from monitor.alerts.alert_agent import AlertEscalationAgent


class OnboardingScheduler:
    def __init__(self) -> None:
        self.bot = MaxMessengerClient()
        self.alerts = AlertEscalationAgent()

    async def process_daily_timeline(self) -> int:
        """Ежедневная CRON-задача: проверяет стаж новичков и отправляет триггерные карточки/пульс-опросы."""
        sent_count = 0
        today = date.today()

        async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            emp_cursor = await db.execute("SELECT * FROM employees WHERE is_active = 1")
            employees = await emp_cursor.fetchall()

            for emp in employees:
                hire_date = date.fromisoformat(emp["hire_date"])
                days_since_hire = (today - hire_date).days

                tmpl_cursor = await db.execute(
                    """
                    SELECT * FROM onboarding_templates
                    WHERE day_offset = ? AND (plant_site = 'ALL' OR plant_site = ?)
                    """,
                    (days_since_hire, emp["plant_site"]),
                )
                templates = await tmpl_cursor.fetchall()

                for tmpl in templates:
                    # Проверяем, не отправляли ли уже этот шаг
                    chk = await db.execute(
                        "SELECT 1 FROM sent_triggers WHERE employee_id = ? AND template_id = ?",
                        (emp["id"], tmpl["id"]),
                    )
                    if await chk.fetchone():
                        continue

                    buttons = json.loads(tmpl["buttons_json"]) if tmpl["buttons_json"] else None
                    await self.bot.send_message(
                        chat_id=emp["max_user_id"],
                        text=f"👋 **{tmpl['title']}**\n\n{tmpl['message_text']}",
                        buttons=buttons,
                        attachment_url=tmpl["attachment_path"],
                    )

                    await db.execute(
                        "INSERT INTO sent_triggers (employee_id, template_id) VALUES (?, ?)",
                        (emp["id"], tmpl["id"]),
                    )
                    sent_count += 1

                    # Если ключевая точка (7 дней или 30 дней) — пуш руководителю подразделения (Роль 03)
                    if days_since_hire in (7, 30, 85) and emp["manager_max_id"]:
                        await self.alerts.notify_department_manager(
                            manager_max_id=emp["manager_max_id"],
                            employee_name=emp["full_name"],
                            stage=tmpl["title"],
                        )

            await db.commit()
        return sent_count