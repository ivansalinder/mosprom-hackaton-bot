from monitor.bot.delivery import MaxMessengerClient
from monitor.config import settings


class AlertEscalationAgent:
    def __init__(self) -> None:
        self.bot = MaxMessengerClient()

    async def escalate_to_hr(
        self,
        employee_max_id: str,
        employee_name: str,
        plant_site: str,
        reason: str,
        user_message: str,
        topic_tag: str,
        risk_score: float,
    ) -> None:
        alert_text = (
            f"🚨 **КРАСНЫЙ ФЛАГ / ЭСКАЛАЦИЯ ОНБОРДИНГА**\n"
            f"👤 **Сотрудник:** {employee_name} (`{employee_max_id}`)\n"
            f"🏭 **Площадка:** {plant_site}\n"
            f"🏷 **Категория:** {topic_tag} (Риск: {risk_score:.2f})\n"
            f"⚠️ **Причина:** {reason}\n"
            f"💬 **Сообщение сотрудника:** «{user_message}»"
        )
        buttons = [
            {"text": "💬 Подключиться к диалогу", "callback_data": f"connect_user:{employee_max_id}"},
            {"text": "✅ Отметить решенным", "callback_data": f"resolve:{employee_max_id}"},
        ]
        await self.bot.send_message(
            chat_id=settings.HR_ESCALATION_CHAT_ID,
            text=alert_text,
            buttons=buttons,
        )

    async def notify_department_manager(self, manager_max_id: str, employee_name: str, stage: str) -> None:
        """Уведомление для Роли 03 (Руководитель подразделения) о проведении 1-on-1 встречи."""
        text = (
            f"🔔 **Напоминание руководителю**\n"
            f"Ваш новый сотрудник **{employee_name}** перешел на этап онбординга: `{stage}`.\n"
            f"Рекомендуем провести короткую личную встречу (15 минут) для обсуждения первых задач и адаптации."
        )
        await self.bot.send_message(chat_id=manager_max_id, text=text)