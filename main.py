from contextlib import asynccontextmanager
from pathlib import Path
import shutil
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from monitor.db import init_db, log_interaction
from monitor.rag.indexer import DocumentIndexer
from monitor.rag.engine import RAGEngine
from monitor.agents.nlu import SentimentNLUAgent
from monitor.agents.summarizer import DigestSummarizer
from monitor.alerts.alert_agent import AlertEscalationAgent
from monitor.bot.delivery import MaxMessengerClient
from monitor.pipeline.scheduler import OnboardingScheduler
from monitor.config import settings

rag_engine = RAGEngine()
nlu_agent = SentimentNLUAgent()
digest_agent = DigestSummarizer()
alert_agent = AlertEscalationAgent()
max_bot = MaxMessengerClient()
onboarding_scheduler = OnboardingScheduler()
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await rag_engine.ensure_loaded()
    # CRON: проверка таймлайна онбординга каждый день в 09:30
    scheduler.add_job(onboarding_scheduler.process_daily_timeline, "cron", hour=9, minute=30)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="Корсик 2.0 — ИИ-ассистент онбординга в МАКС (Группа ПОЛИПЛАСТИК)",
    lifespan=lifespan,
)


class MaxWebhookUpdate(BaseModel):
    user_id: str
    user_name: str = "Новый сотрудник"
    plant_site: str = "Очаково (Москва)"
    message_text: str = ""
    callback_data: str | None = None
    is_pulse_response: bool = False


@app.post("/webhook/max")
async def handle_max_webhook(update: MaxWebhookUpdate):
    """Обработчик входящих сообщений и нажатий кнопок из мессенджера МАКС."""
    # 1. Ручная эскалация по кнопке
    if update.callback_data == "escalate_hr":
        await alert_agent.escalate_to_hr(
            employee_max_id=update.user_id,
            employee_name=update.user_name,
            plant_site=update.plant_site,
            reason="Прямой запрос связи с HR через меню",
            user_message=update.message_text or "[Нажата кнопка вызова HR]",
            topic_tag="запрос HR",
            risk_score=0.5,
        )
        msg = "👩‍💼 Я передал ваш запрос HR-специалисту. Он напишет вам в личные сообщения в «МАКС» в ближайшее время!"
        await max_bot.send_message(chat_id=update.user_id, text=msg)
        return {"status": "escalated"}

    # 2. Предустановленные быстрые кнопки гибридного меню -> переводим в вопрос к RAG
    quick_queries = {
        "faq_schedule": "Какой режим рабочего времени, начало и окончание работы и перерыв на обед?",
        "faq_salary": "В какие даты выплачивается заработная плата и аванс?",
        "faq_vacation": "Какая продолжительность отпуска и когда возникает право на него?",
    }
    query_text = quick_queries.get(update.callback_data or "", update.message_text)

    # 3. Sentiment Analysis (анализ настроений и детекция «красных флагов»)
    nlu_result = await nlu_agent.analyze_message(query_text)

    # Если это ответ на пульс-опрос или обнаружен «красный флаг»
    if update.is_pulse_response or nlu_result["is_red_flag"]:
        if nlu_result["is_red_flag"]:
            await alert_agent.escalate_to_hr(
                employee_max_id=update.user_id,
                employee_name=update.user_name,
                plant_site=update.plant_site,
                reason=f"ИИ выявил красный флаг ({nlu_result['sentiment']}): {nlu_result['hr_summary']}",
                user_message=query_text,
                topic_tag=nlu_result["topic_tag"],
                risk_score=nlu_result["risk_score"],
            )
        if update.is_pulse_response:
            thanks_msg = "Спасибо за честную обратную связь! Ваши ответы помогают нам делать адаптацию комфортнее."
            await log_interaction(
                max_user_id=update.user_id,
                interaction_type="pulse_answer",
                user_text=query_text,
                bot_response=thanks_msg,
                topic_tag=nlu_result["topic_tag"],
                sentiment=nlu_result["sentiment"],
                risk_score=nlu_result["risk_score"],
                is_red_flag=nlu_result["is_red_flag"],
                escalated_to_hr=nlu_result["is_red_flag"],
            )
            await max_bot.send_message(chat_id=update.user_id, text=thanks_msg)
            return {"status": "pulse_recorded", "nlu": nlu_result}

    # 4. Ответ на вопрос по регламентам через RAG
    rag_res = await rag_engine.answer_question(query_text, plant_site=update.plant_site)

    if rag_res["needs_escalation"] and not nlu_result["is_red_flag"]:
        await alert_agent.escalate_to_hr(
            employee_max_id=update.user_id,
            employee_name=update.user_name,
            plant_site=update.plant_site,
            reason=f"Низкая уверенность RAG ({rag_res['confidence']}) — защита от галлюцинаций",
            user_message=query_text,
            topic_tag=nlu_result["topic_tag"],
            risk_score=nlu_result["risk_score"],
        )

    await log_interaction(
        max_user_id=update.user_id,
        interaction_type="question",
        user_text=query_text,
        bot_response=rag_res["answer"],
        topic_tag=nlu_result["topic_tag"],
        sentiment=nlu_result["sentiment"],
        risk_score=nlu_result["risk_score"],
        is_red_flag=nlu_result["is_red_flag"],
        escalated_to_hr=rag_res["needs_escalation"] or nlu_result["is_red_flag"],
    )

    await max_bot.send_message(
        chat_id=update.user_id,
        text=rag_res["answer"],
        buttons=max_bot.default_hybrid_menu(),
    )
    return {
        "answer": rag_res["answer"],
        "sources": rag_res["sources"],
        "confidence": rag_res["confidence"],
        "needs_escalation": rag_res["needs_escalation"],
        "sentiment": nlu_result,
    }


@app.post("/api/kb/upload")
async def upload_knowledge_base_document(file: UploadFile = File(...)):
    """Эндпоинт для HR: загрузка нового PDF-регламента и переиндексация RAG без написания кода."""
    if not file.filename or not file.filename.lower().endswith((".pdf", ".txt")):
        raise HTTPException(status_code=400, detail="Поддерживаются файлы .pdf и .txt")
    dest = Path(settings.DOCS_DIR) / file.filename
    with dest.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    indexer = DocumentIndexer()
    chunks_count = await indexer.rebuild_index()
    await rag_engine.ensure_loaded()
    return {"status": "indexed", "filename": file.filename, "total_chunks": chunks_count}


@app.get("/api/hr/digest")
async def get_weekly_digest():
    """Еженедельный дайджест «Настроения в коллективе новичков» (для Роли 04)."""
    digest = await digest_agent.build_weekly_hr_digest()
    return {"digest": digest}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)