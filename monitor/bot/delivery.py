from typing import List, Dict, Any, Optional
import httpx
from monitor.config import settings


class MaxMessengerClient:
    """Интеграционный слой для API корпоративного мессенджера «МАКС»."""

    def __init__(self) -> None:
        self.base_url = settings.MAX_API_BASE_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.MAX_BOT_TOKEN}",
            "Content-Type": "application/json",
        }

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: Optional[List[Dict[str, str]]] = None,
        attachment_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "format": "markdown",
        }
        attachments = []
        if buttons:
            attachments.append({
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [{"type": "callback", "text": b["text"], "payload": b["callback_data"]}]
                        for b in buttons
                    ]
                },
            })
        if attachment_url:
            attachments.append({
                "type": "file",
                "payload": {"url": attachment_url},
            })
        if attachments:
            payload["attachments"] = attachments

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/messages",
                    headers=self.headers,
                    json=payload,
                )
                return {"ok": resp.status_code < 400, "status": resp.status_code}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @staticmethod
    def default_hybrid_menu() -> List[Dict[str, str]]:
        """Гибридное меню: быстрые кнопки + возможность писать текстом."""
        return [
            {"text": "📅 График работы и обед", "callback_data": "faq_schedule"},
            {"text": "💳 Даты выплаты зарплаты", "callback_data": "faq_salary"},
            {"text": "🏖 Как оформить отпуск", "callback_data": "faq_vacation"},
            {"text": "👩‍💼 Позвать HR-специалиста", "callback_data": "escalate_hr"},
        ]