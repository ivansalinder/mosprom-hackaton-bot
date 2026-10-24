import aiosqlite
from openai import AsyncOpenAI
from monitor.config import settings, get_llm_client


class DigestSummarizer:
    def __init__(self) -> None:
        self.client = get_llm_client()

    async def build_weekly_hr_digest(self) -> str:
        async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT user_text, topic_tag, sentiment, is_red_flag, risk_score
                FROM interactions
                WHERE created_at >= datetime('now', '-7 days')
                """
            )
            rows = await cursor.fetchall()

        if not rows:
            return "📊 За последние 7 дней новых обращений и ответов в пульс-опросах не зафиксировано."

        total = len(rows)
        red_flags = [dict(r) for r in rows if r["is_red_flag"]]
        records_dump = "\n".join(
            [f"- [{r['sentiment']} | тег: {r['topic_tag']} | риск: {r['risk_score']}] {r['user_text']}" for r in rows[:50]]
        )

        prompt = (
            f"Всего обращений/ответов новичков за неделю: {total}, из них красных флагов: {len(red_flags)}.\n"
            f"Сырые данные:\n{records_dump}\n\n"
            "Сформируй еженедельный управленческий дайджест «Настроения в коллективе новичков» для Руководителя управления "
            "по подбору и развитию персонала Группы ПОЛИПЛАСТИК:\n"
            "1. Общий индекс настроений.\n"
            "2. Кластеризация ключевых проблем (доступы, наставники, регламенты и т.д.).\n"
            "3. Зоны риска (красные флаги), требующие превентивного вмешательства HR."
        )

        resp = await self.client.chat.completions.create(
            model=settings.LLM_MODEL,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or "Дайджест сформирован."