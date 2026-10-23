import json
from pathlib import Path
from typing import Dict, Any, List
import numpy as np
from openai import AsyncOpenAI
from monitor.config import settings
from monitor.rag.indexer import DocumentIndexer


SYSTEM_PROMPT_RAG = """Ты — «Корсик», корпоративный ИИ-ассистент по адаптации персонала ООО «Группа ПОЛИПЛАСТИК» в мессенджере «МАКС».
Твоя задача — помогать новым сотрудникам быстро находить точные ответы по регламентам компании.

СТРОГИЕ ПРАВИЛА БЕЗОПАСНОСТИ (КОНТРОЛЬ ГАЛЛЮЦИНАЦИЙ):
1. Отвечай ТОЛЬКО на основе предоставленного ниже КОНТЕКСТА из официальных документов компании.
2. Запрещено придумывать кадровые, юридические или финансовые правила, ссылки, даты или номера кабинетов, если их нет в контексте.
3. Обязательно указывай источник (например: «Согласно ПВТР П 3.11-07, п. 6.6.1...»).
4. Если в контексте нет точного ответа на вопрос пользователя, напиши ровно одну фразу: «NEED_HR_ESCALATION».
5. Соблюдай дружелюбный, поддерживающий корпоративный стиль общения."""


class RAGEngine:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        self.index_path = Path(settings.RAG_INDEX_PATH)
        self.chunks: List[Dict[str, Any]] = []

    async def ensure_loaded(self) -> None:
        if not self.index_path.exists():
            indexer = DocumentIndexer()
            await indexer.rebuild_index()
        self.chunks = json.loads(self.index_path.read_text(encoding="utf-8"))

    async def answer_question(self, query: str, plant_site: str = "Москва") -> Dict[str, Any]:
        await self.ensure_loaded()
        if not self.chunks:
            return {
                "answer": "База знаний обновляется. Переключаю ваш вопрос на HR-специалиста.",
                "needs_escalation": True,
                "sources": [],
                "confidence": 0.0,
            }

        # 1. Получаем эмбеддинг запроса
        emb_resp = await self.client.embeddings.create(model=settings.EMBEDDING_MODEL, input=[query])
        q_vec = np.array(emb_resp.data[0].embedding, dtype=np.float32)

        # 2. Косинусная близость с чанками
        scored = []
        for chunk in self.chunks:
            c_vec = np.array(chunk["embedding"], dtype=np.float32)
            denom = (np.linalg.norm(q_vec) * np.linalg.norm(c_vec)) + 1e-9
            score = float(np.dot(q_vec, c_vec) / denom)
            scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_matches = scored[:3]
        best_score = top_matches[0][0] if top_matches else 0.0

        # Проверка порога уверенности (защита от галлюцинаций)
        if best_score < settings.RAG_SIMILARITY_THRESHOLD:
            return {
                "answer": (
                    "Я не нашел точного регламента по вашему вопросу в базе знаний для площадки "
                    f"«{plant_site}». Чтобы не вводить вас в заблуждение, я передал вопрос вашему HR-менеджеру — "
                    "он скоро подключится к диалогу."
                ),
                "needs_escalation": True,
                "sources": [],
                "confidence": round(best_score, 3),
            }

        context_text = "\n\n---\n\n".join(
            [f"Источник: {c['source']} (стр. {c['page']})\nТекст: {c['text']}" for s, c in top_matches]
        )

        # 3. Генерация ответа со строгим ограничением контекста
        completion = await self.client.chat.completions.create(
            model=settings.LLM_MODEL,
            temperature=0.0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_RAG},
                {
                    "role": "user",
                    "content": f"Площадка сотрудника: {plant_site}\n\nКОНТЕКСТ:\n{context_text}\n\nВОПРОС СОТРУДНИКА:\n{query}",
                },
            ],
        )
        answer = (completion.choices[0].message.content or "").strip()

        if "NEED_HR_ESCALATION" in answer:
            return {
                "answer": (
                    "Этот вопрос требует индивидуальной проверки кадровым специалистом. "
                    "Я уже направил уведомление HR-менеджеру в «МАКС»!"
                ),
                "needs_escalation": True,
                "sources": [c["source"] for _, c in top_matches],
                "confidence": round(best_score, 3),
            }

        return {
            "answer": answer,
            "needs_escalation": False,
            "sources": list({f"{c['source']} (стр. {c['page']})" for _, c in top_matches}),
            "confidence": round(best_score, 3),
        }