# Внешние интеграции

## Реестр

| Интеграция | Тип | Назначение | Доступ/авторизация | Текущее состояние |
|---|---|---|---|---|
| MAX Bot API через `maxapi` | Внешний мессенджер API | Long polling, текст, inline keyboard, callbacks | `MAX_BOT_TOKEN` из `.env` | Активная интеграция; ошибки доставки не оборачиваются в общий retry на уровне адаптера |
| SQLite через `aiosqlite` | БД | FSM, сотрудники, timeline, опросы, тикеты, логи, экспорт | Локальный файл по `SQLITE_DB_PATH` | Активная интеграция |
| HR уведомления MAX | MAX chats | Ticket, red flag, weekly digest | `HR_ESCALATION_CHAT_ID`, `HEAD_HR_CHAT_ID` | При пустом ID уведомление логируется как заглушка |
| OpenAI-compatible API | LLM + embeddings | RAG ответы / embeddings; mock AI интерфейс | `LLM_API_KEY`, `LLM_BASE_URL`, модельные настройки | `ENABLE_RAG=false` по умолчанию; без действующего ключа выдаётся локальный mock |
| Локальный HRIS mock | JSON-файл | Создание демо-профилей и поиск по offer-коду | Без внешней авторизации | `mock_hris.json`; синтетические данные |
| YAML-контент | Локальные файлы | Тексты и вопросы опросов | Нет | Загружается при создании `KorsikFlows` |

## MAX и маршрутизация

`MaxAdapter` создаёт `Bot`/`Dispatcher`, регистрирует update handlers, преобразует сообщения в `IncomingMessage` и callbacks в `IncomingCallback`; дальше бизнес-логику исполняет `KorsikFlows`. Кнопка MAX содержит текст и `payload`; обработчик callback вызывает `ack()` даже при исключении. Отдельная реализация `monitor/bot/delivery.py` использует прямой `httpx`, но относится к старому пути и не вызывается текущей точкой входа.

## База данных

- SQLite подключается асинхронно, но множество небольших запросов открывают отдельные соединения.
- Схема находится в `db/schema.sql`; `monitor/db.py` расширяет существующую legacy-схему через `PRAGMA table_info` и `ALTER TABLE`.
- В `employees` различаются `employee_code` (offer-код для регистрации), `employee_id` (псевдоним для аналитики) и `max_user_id`.
- PostgreSQL/pgvector, упомянутые как опция в исходном ТЗ, не реализованы.

## Секреты

`.env.example` перечисляет имена настроек без production credentials. `monitor/config.py` загружает `.env`; MAX-адаптер требует заполненный токен. Реальные токены не включать в документацию, коммиты и логи. Ротация, хранение в secrets manager и production-политика неизвестны.

## Поведение при сбоях и видимость

- APScheduler выполняет timeline и напоминания внутри того же процесса.
- Ошибка отправки timeline stage логируется и повторяется до лимита попыток (три).
- Общие повторные попытки/обработка сетевых сбоев в `MaxAdapter` не определены в прикладном коде; зависят от библиотеки.
- Нет метрик, tracing или отдельного monitoring backend в активном коде.
- Уведомления о проблемах интеграции представлены стандартными логами.

## Источники

- `monitor/bot/max_adapter.py`, `monitor/bot/delivery.py`
- `monitor/config.py`, `.env.example`
- `monitor/db.py`, `monitor/storage.py`, `db/schema.sql`
- `monitor/rag/engine.py`, `monitor/rag/indexer.py`
- `main.py`, `mock_hris.json`
