from pathlib import Path

from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _normalize_path(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)
    return str((PROJECT_ROOT / candidate).resolve())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MAX_API_BASE_URL: str = "https://botapi.max.ru"
    MAX_BOT_TOKEN: str = ""
    HR_ESCALATION_CHAT_ID: str = ""
    HEAD_HR_CHAT_ID: str = ""
    TIMEZONE: str = "Europe/Moscow"
    TIME_SCALE: float = 1.0
    BINDING_METHOD: str = "offer_code"
    HR_SLA_HOURS: int = 4
    RAG_MIN_CONFIDENCE: float = 0.65
    ENABLE_RAG: bool = False
    ADMIN_USER_IDS: str = ""
    DEMO_MODE: bool = True
    RECRUITER_URL: str = ""
    SHOW_ASSET_STUBS: bool = True
    IT_ESCALATION_CHAT_ID: str = ""

    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    DOCS_DIR: str = "data/docs"
    RAG_INDEX_PATH: str = "data/rag_index.json"
    RAG_SIMILARITY_THRESHOLD: float = 0.42

    SQLITE_DB_PATH: str = "data/korsik.db"

    @property
    def resolved_project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def resolved_mock_hris_path(self) -> Path:
        return PROJECT_ROOT / "mock_hris.json"

    @property
    def resolved_content_path(self) -> Path:
        return PROJECT_ROOT / "content" / "messages.yaml"

    @property
    def resolved_assets_dir(self) -> Path:
        return PROJECT_ROOT / "assets"

    @property
    def resolved_docs_dir(self) -> Path:
        return Path(_normalize_path(self.DOCS_DIR))

    @property
    def resolved_rag_index_path(self) -> Path:
        return Path(_normalize_path(self.RAG_INDEX_PATH))

    @property
    def resolved_sqlite_db_path(self) -> Path:
        return Path(_normalize_path(self.SQLITE_DB_PATH))


settings = Settings()


class MockEmbeddings:
    async def create(self, model, input, **kwargs):
        class DummyItem:
            def __init__(self):
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
                if is_json:
                    self.content = '{"sentiment": "neutral", "topic_tag": "тест-заглушка", "risk_score": 0.1, "is_red_flag": false, "hr_summary": "тестовое саммари"}'
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
    """Use real OpenAI-compatible client if API key is configured; otherwise use the local mock."""
    api_key = (settings.LLM_API_KEY or "").strip()
    if api_key and api_key not in {"your_llm_api_key", "changeme", "test"}:
        return AsyncOpenAI(api_key=api_key, base_url=settings.LLM_BASE_URL or None)
    return MockAsyncOpenAI()
