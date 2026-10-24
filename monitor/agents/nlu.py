import json
from typing import Dict, Any
from openai import AsyncOpenAI
from monitor.config import settings, get_llm_client

NLU_SYSTEM_PROMPT = """Ты — NLP-модуль эмоционального интеллекта (Sentiment Analysis) HR-платформы Группы ПОЛИПЛАСТИК.
Проанализируй сообщение нового сотрудника (ответ на пульс-опрос или обращение в бот) и верни строго JSON:
{
  "sentiment": "positive" | "neutral" | "negative" | "critical",
  "topic_tag": "проблемы с доступами" | "конфликт с наставником" | "непонимание задач" | "токсичность в коллективе" | "график и отпуск" | "документы и КЭДО" | "зарплата" | "общее",
  "risk_score": число от 0.0 до 1.0 (вероятность демотивации или увольнения),
  "is_red_flag": true | false (true, если есть токсичность, конфликт, отсутствие доступов более 2 дней, мысли об увольнении или прямой запрос HR),
  "hr_summary": "Краткая выжимка сути проблемы для HR-менеджера в 1 предложении"
}"""


class SentimentNLUAgent:
    def __init__(self) -> None:
        self.client = get_llm_client()

    async def analyze_message(self, text: str) -> Dict[str, Any]:
        try:
            resp = await self.client.chat.completions.create(
                model=settings.LLM_MODEL,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": NLU_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return {
                "sentiment": data.get("sentiment", "neutral"),
                "topic_tag": data.get("topic_tag", "общее"),
                "risk_score": float(data.get("risk_score", 0.0)),
                "is_red_flag": bool(data.get("is_red_flag", False)),
                "hr_summary": data.get("hr_summary", ""),
            }
        except Exception:
            # Fallback при недоступности LLM
            lower = text.lower()
            red_keywords = ["уволить", "увольняюсь", "конфликт", "игнорируют", "нет доступа", "токсич", "не понимаю"]
            is_red = any(k in lower for k in red_keywords)
            return {
                "sentiment": "negative" if is_red else "neutral",
                "topic_tag": "общее",
                "risk_score": 0.8 if is_red else 0.1,
                "is_red_flag": is_red,
                "hr_summary": text[:120],
            }