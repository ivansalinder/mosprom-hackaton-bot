# Технологический стек

## Кратко

| Область | Фактическая реализация | Источник |
|---|---|---|
| Язык | Python | `pyproject.toml`, `main.py` |
| Минимальная версия Python | `>=3.11` | `pyproject.toml` |
| Управление зависимостями | `uv`; зафиксированные версии в `uv.lock` | `pyproject.toml`, `uv.lock` |
| MAX-бот | `maxapi` с long polling; зависимость берётся из соседней папки `../maxapi` | `pyproject.toml`, `monitor/bot/max_adapter.py` |
| Хранилище | SQLite через `aiosqlite` | `monitor/db.py`, `monitor/storage.py`, `db/schema.sql` |
| Планировщик | `APScheduler` `AsyncIOScheduler` | `main.py` |
| Контент | YAML, загружается через PyYAML | `content/messages.yaml`, `content/surveys.yaml`, `monitor/flows.py` |
| Настройки | Pydantic Settings; `.env` и значения по умолчанию | `monitor/config.py`, `.env.example` |
| Совместимый с OpenAI клиент | `openai`; локальная тестовая заглушка используется без действующего API-ключа | `monitor/config.py` |
| Индексация документов | `pypdf`, JSON-индекс и `numpy` для cosine similarity | `monitor/rag/indexer.py`, `monitor/rag/engine.py` |

## Основные зависимости

| Зависимость | Объявленный диапазон | Назначение |
|---|---:|---|
| `maxapi` | Локальная зависимость | MAX polling, команды, callback-кнопки, отправка сообщений |
| `aiosqlite` | `>=0.20.0` | Асинхронные запросы к SQLite |
| `apscheduler` | `>=3.10.4` | Интервальные и CRON-задачи |
| `pydantic-settings` | `>=2.5.0` | Настройки из окружения и `.env` |
| `PyYAML` | `>=6.0.2` | Контент сообщений и опросов |
| `openai` | `>=1.50.0` | OpenAI-compatible API для RAG и прототипных AI-модулей |
| `pypdf` | `>=5.0.0` | Извлечение текста из PDF в индексаторе |
| `numpy` | `>=1.26.0` | Сравнение embedding-векторов |
| `fastapi`, `uvicorn`, `httpx`, `pydantic`, `openpyxl`, `python-multipart` | См. `pyproject.toml` | Заявлены в зависимостях; основной запуск из `main.py` ими напрямую не пользуется |

## Команды

Запуск из каталога `mosprom-hackaton-bot` в PowerShell:

```powershell
uv sync
uv run python .\main.py
```

Проверки:

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q main.py monitor tests
uv lock --check
```

Текущие проверки не используют отдельные линтер или форматтер: конфигурация таких инструментов в проекте не обнаружена.

## Конфигурация окружения

Основные переменные из `.env.example`: `MAX_BOT_TOKEN`, `MAX_API_BASE_URL`, `HR_ESCALATION_CHAT_ID`, `HEAD_HR_CHAT_ID`, `ADMIN_USER_IDS`, `TIMEZONE`, `TIME_SCALE`, `BINDING_METHOD`, `DEMO_MODE`, `HR_SLA_HOURS`, `ENABLE_RAG`, `RAG_MIN_CONFIDENCE`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`, `SQLITE_DB_PATH`.

Путь к базе, HRIS и YAML формируется в `monitor/config.py`. Относительный `SQLITE_DB_PATH` считается от корня проекта. Секреты в документации и исходном коде не хранятся; рабочий MAX-токен задаётся локально в `.env`.

## Ограничения исполнения

- Для старта нужен MAX Bot API token: `MaxAdapter.__init__` завершает запуск ошибкой, если `MAX_BOT_TOKEN` пуст.
- Рабочая схема запуска — один процесс polling-бота плюс встроенный asyncio scheduler.
- Текущее хранилище — локальная SQLite, не PostgreSQL.
- `ENABLE_RAG` по умолчанию выключен; без настроенного LLM API `get_llm_client()` выдаёт mock-ответы.
- Локальная ссылка на `../maxapi` означает, что при переносе проекта нужна соседняя библиотека или замена источника зависимости.

## Источники

- `pyproject.toml`, `uv.lock`
- `.env.example`, `monitor/config.py`
- `main.py`, `monitor/bot/max_adapter.py`
- `monitor/db.py`, `db/schema.sql`
- `monitor/rag/engine.py`, `monitor/rag/indexer.py`
