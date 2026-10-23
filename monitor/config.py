from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Настройки мессенджера «МАКС»
    MAX_API_BASE_URL: str = "https://botapi.max.ru"
    MAX_BOT_TOKEN: str = "your_max_bot_token"
    HR_ESCALATION_CHAT_ID: str = "hr_alerts_channel_id"

    # Настройки LLM и Embeddings (OpenAI-совместимый API: локальный vLLM / GigaChat / OpenAI)
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = "your_llm_api_key"
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Настройки RAG и контроля галлюцинаций
    DOCS_DIR: str = "data/docs"
    RAG_INDEX_PATH: str = "data/rag_index.json"
    RAG_SIMILARITY_THRESHOLD: float = 0.42  # Ниже порога -> эскалация на HR, не выдумываем ответ

    # БД
    SQLITE_DB_PATH: str = "data/korsik.db"


settings = Settings()