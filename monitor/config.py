from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Настройки мессенджера «МАКС»
    MAX_API_BASE_URL: str = "https://botapi.max.ru"
    MAX_BOT_TOKEN: str = "your_max_bot_token"
    HR_ESCALATION_CHAT_ID: str = "hr_alerts_channel_id"

    # Настройки LLM и Embeddings
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = "your_llm_api_key"
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Настройки RAG и контроля галлюцинаций
    DOCS_DIR: str = "data/docs"
    RAG_INDEX_PATH: str = "data/rag_index.json"
    RAG_SIMILARITY_THRESHOLD: float = 0.42

    # БД
    SQLITE_DB_PATH: str = "data/korsik.db"


settings = Settings()


# --- Локальная заглушка (Mock) для LLM ---

class MockEmbeddings:
    async def create(self, model, input, **kwargs):
        class DummyItem:
            def __init__(self):
                # Возвращаем фейковый вектор нужной размерности
                self.embedding = [0.01] * 1536 
        class DummyResponse:
            def __init__(self, count):
                self.data = [DummyItem() for _ in range(count)]
        
        count = len(input) if isinstance(input, list) else 1
        return DummyResponse(count)

class MockChatCompletions:
    async def create(self, messages, **kwargs):
        class DummyMessage:
            def __init__(self, is_json):
                # Если просят JSON - отдаем валидный JSON
                if is_json:
                    self.content = '{"sentiment": "neutral", "topic_tag": "тест-заглушка", "risk_score": 0.1, "is_red_flag": false, "hr_summary": "тестовое саммари"}'
                # Иначе отдаем обычный текст для RAG
                else:
                    self.content = "Это тестовый ответ-заглушка (LLM временно отключена для отладки). Согласно регламенту, все работает штатно."
        
        class DummyChoice:
            def __init__(self, is_json):
                self.message = DummyMessage(is_json)
        
        class DummyResponse:
            def __init__(self, is_json):
                self.choices = [DummyChoice(is_json)]
        
        is_json = kwargs.get("response_format", {}).get("type") == "json_object"
        return DummyResponse(is_json)

class MockChat:
    def __init__(self):
        self.completions = MockChatCompletions()

class MockAsyncOpenAI:
    def __init__(self, **kwargs):
        self.embeddings = MockEmbeddings()
        self.chat = MockChat()


def get_llm_client():
    """Возвращает локальную заглушку для быстрого старта и тестирования логики"""
    return MockAsyncOpenAI()