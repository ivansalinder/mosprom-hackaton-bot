import aiosqlite
from pathlib import Path
from monitor.config import settings


async def init_db() -> None:
    Path(settings.SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema_path = Path("db/schema.sql")
    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        if schema_path.exists():
            await db.executescript(schema_path.read_text(encoding="utf-8"))
            await db.commit()


async def log_interaction(
    max_user_id: str,
    interaction_type: str,
    user_text: str,
    bot_response: str,
    topic_tag: str = "общее",
    sentiment: str = "neutral",
    risk_score: float = 0.0,
    is_red_flag: bool = False,
    escalated_to_hr: bool = False,
) -> int:
    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO interactions 
            (max_user_id, interaction_type, user_text, bot_response, topic_tag, sentiment, risk_score, is_red_flag, escalated_to_hr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                max_user_id,
                interaction_type,
                user_text,
                bot_response,
                topic_tag,
                sentiment,
                risk_score,
                int(is_red_flag),
                int(escalated_to_hr),
            ),
        )
        await db.commit()
        return cursor.lastrowid