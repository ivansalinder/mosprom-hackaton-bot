# Архитектура и контекст для дальнейшей работы

## Назначение продукта

«Корсик» — демо-бот MAX, который сопровождает нового сотрудника в течение pre-boarding и первых примерно 90 дней работы. Код построен как Python-приложение с long polling, SQLite, YAML-контентом и синтетическим HRIS. Это прототип, не production-интеграция с HRIS и не готовый BI/AI-сервис.

Главные активные модули:

- `main.py` — запуск и фоновые расписания;
- `monitor/bot/max_adapter.py` — интеграция MAX;
- `monitor/flows.py` — сообщения, FSM и сценарная логика;
- `monitor/storage.py`, `monitor/db.py` — запись и чтение SQLite;
- `monitor/timeline.py` — таймлайн;
- `monitor/analytics.py` — выгрузки.

## Что происходит после `uv run python .\main.py`

1. **Загрузка модулей и конфигурации.** Импорт `monitor.config` создаёт экземпляр `Settings`, читает локальный `.env` и вычисляет корень проекта. Для SQLite, HRIS и content-файлов задаются абсолютные пути от проекта.
2. **Инициализация БД.** `main()` вызывает `init_db()`: создаёт родительскую папку базы, исполняет `db/schema.sql`, проверяет таблицы старой базы и добавляет отсутствующие столбцы/индексы, затем upsert-ит синтетические записи из `mock_hris.json`. У `employee_id` — псевдоним `E-000N`, `employee_code` — offer-код для поиска. Это разные значения.
3. **Создание интеграционного слоя.** `MaxAdapter()` проверяет `MAX_BOT_TOKEN`; пустое значение приводит к `RuntimeError`. Затем создаётся `KorsikFlows(adapter)`, который загружает `content/messages.yaml` и `content/surveys.yaml`, создаёт `TimelineScheduler` и `RAGEngine`.
4. **Подключение событий.** `adapter.bind(...)` связывает команды `/start`, `/Food`, `/HR`, `/close`, `/simulate`, `/approve`, `/export_analytics`, свободный текст, callback-кнопки и событие первого запуска MAX с методами `KorsikFlows`.
5. **Запуск расписаний.** `AsyncIOScheduler` в часовом поясе `TIMEZONE` вызывает `timeline.run_due()` каждые 15 секунд; опросные напоминания и SLA — каждую минуту; агрегированный HR-дайджест — по понедельникам в 09:00.
6. **Polling.** `scheduler.start()` запускает фоновые задачи, после чего `await adapter.run()` ждёт события MAX. При остановке выполняется `scheduler.shutdown()` и закрывается сессия MAX.

## Поток пользователя: от `/start` до таймлайна

```text
MAX update
  -> MaxAdapter handler
  -> IncomingMessage / IncomingCallback
  -> KorsikFlows
  -> storage/db + content YAML
  -> MaxAdapter.send(...)
```

### Регистрация

1. `CommandStart` в адаптере вызывает `flows.start()`. Для события `bot_started` используется такой же путь.
2. Если MAX user ID уже связан с сотрудником и `onboarding_state == "registered"`, бот показывает приветствие и меню.
3. Иначе он ставит сохранённое состояние `awaiting_code` и отправляет текст знакомства, TODO-плейсхолдеры для отсутствующих картинок и просьбу ввести код из оффера.
4. Следующее текстовое сообщение маршрутизируется через `flows.message()`. Командные сообщения проверяются первыми, потом считывается состояние FSM; при `awaiting_code` вызывается `_check_binding_code()`.
5. Код ищется без учёта регистра в `employees.employee_code`. Seed этого поля берётся из `mock_hris.json` → `binding_code`. Пример демо-кода — `DEMO-GTP-OFFICE`; это не внутренний псевдоним `E-0003`.
6. При совпадении сохраняется FSM-состояние `confirm_employee`; бот показывает демо-данные и callback-кнопки «Подтверждаю» / «Изменить». Неверный код приводит к сообщению с кнопкой обращения к рекрутеру.
7. При подтверждении MAX ID связывается с записью сотрудника, состояние становится `choose_address_form`; затем callback «На ТЫ» или «На ВЫ» сохраняет форму, переводит сотрудника в `registered` и создаёт 25 timeline jobs.
8. После регистрации бот показывает меню и отправляет HR-уведомление. Если HR chat ID не задан, адаптер только пишет предупреждение в лог.

FSM хранится в `conversation_states` в SQLite, а не в памяти процесса. Таблица хранит `state`, JSON `data_json` и timestamp. Основные состояния: `awaiting_code`, `confirm_employee`, `choose_address_form`, `survey`, `survey_text`, `survey_reason`, `self_intro`.

### Обычные сообщения и команды

`KorsikFlows.message()` логирует входящее сообщение, проверяет команды, затем состояние диалога. Текст в режиме `awaiting_code` — код; текст в survey/self-intro состояниях обрабатывается как ожидаемый ответ. При `handoff_active` обычный текст уведомляется HR и не проходит через bot-answer flow. Иначе он проходит через `_answer_free_text()`: чувствительные кадровые/юридические темы эскалируются; все остальные вопросы идут в RAG-ветку. При `ENABLE_RAG=false` `_rag_stub()` возвращает `None`, поэтому стандартное поведение — создать HR handoff, а не выдумать ответ.

Команды:

| Команда | Реальное поведение |
|---|---|
| `/start` | Старт или повторный показ приветствия/меню |
| `/Food` | Сообщение из контента; материалы питания остаются заглушкой |
| `/HR` | Создать тикет, включить handoff и уведомить HR |
| `/close [employee_code]` | Закрыть тикет; команда ограничена `ADMIN_USER_IDS` |
| `/simulate <stage>` | Запустить stage для профиля администратора; ограничено `ADMIN_USER_IDS` |
| `/approve <employee_code> <day2\|day3>` | Одобрить этап для указанного сотрудника; ограничено `ADMIN_USER_IDS` |
| `/export_analytics` | Создать 8 CSV-файлов в `exports/`; ограничено `ADMIN_USER_IDS` |

### Кнопки

В callback MAX передаёт строковый `payload`. `MaxAdapter` преобразует его в `IncomingCallback`; `KorsikFlows.callback()` маршрутизирует его по префиксу/значению (`reg:`, `survey:`, `hr:contact`, `menu:*`, `food:*`, `access:*` и др.). Survey callback содержит session ID и индекс вопроса; устаревшие callback отклоняются сообщением из контента.

### Таймлайн

`TimelineScheduler.create_jobs()` добавляет 25 уникальных заданий, привязанных к сотруднику и `stage_key`; существующие пары не дублируются. Расписание описано в `STAGES` в `monitor/timeline.py`: pre-boarding за несколько дней, первый рабочий день, контрольные точки до конца испытательного срока.

Каждые 15 секунд `run_due()` выбирает до 100 задач со статусом `pending`, срок которых наступил. Неактивный профиль отменяет задачу; активный HR handoff откладывает отправку; `self_intro` и `adaptation_plan` требуют предварительную отмашку рекрутера. Ошибки отправки логируются и увеличивают attempts; после трёх попыток задача получает `failed`. Успех вызывает `flows.send_stage()` и помечает задачу `sent`.

`send_stage()` подбирает YAML-текст, параметры и callback-кнопки по `stage_key`; учитывает площадку и категорию работника для ряда этапов. Недостающие изображения, PDF, ссылки и материалы выводятся как TODO-заглушки. Некоторые расписания/фильтры — демо-логика, а не полная реализация всех требований ТЗ.

`TIME_SCALE` масштабирует смещение планового времени относительно hire date. При создании расписания прошедшие цели устанавливаются на текущий момент. `_shift_to_workday()` переносит выходной на понедельник, но не реализует все оговорённые в ТЗ правила рабочего времени.

### Опросы, флаги и HR

- Определения вопросов загружаются из `content/surveys.yaml`.
- `_start_survey()` создаёт `survey_sessions` и сохраняет текущую позицию в FSM.
- `_send_survey_question()` формирует кнопки выбора, шкалу, multi-select или ожидает текст.
- `_survey_callback()` проверяет состояние и индексы, затем вычисляет низкую оценку и сохраняет ответ.
- `_save_answer()` пишет ответ в SQLite и при необходимости вызывает эвристический анализ комментария.
- `_complete_survey()` завершает сессию, рассчитывает средний индекс/низкие ответы и возвращает меню.
- `_analyze_text()` и специальные правила отдельных ответов создают `red_flags`; `_create_red_flag()` записывает флаг, уведомляет HR и может записать manager action.
- `_handoff()` создаёт ticket и блокирует проактивные отправки, пока handoff не закрыт администратором.
- Периодические функции отправляют напоминания о незавершённых опросах и тикетах, а weekly digest агрегирует тикеты, knowledge gaps и флаги.

Анализ sentiment в активном потоке — эвристика по ключевым словам; старый `SentimentNLUAgent` не вызывается `flows.py`. HR и manager уведомления зависят от MAX chat ID; manager push в текущем активном коде обозначен заглушкой и иногда маршрутизируется через HR-notice.

## Файлы и ответственность функций

### `main.py`

- `main()` — `init_db`, создание `MaxAdapter` и `KorsikFlows`, привязка событий, настройка четырёх APScheduler jobs, запуск polling; завершает scheduler и сессию в `finally`.
- Верхнеуровневые `logger` и `logging.basicConfig` задают формат сообщений запуска и ошибок.

### `monitor/config.py`

- `_normalize_path(value)` — превращает относительный путь в абсолютный путь от корня приложения.
- `Settings` — читает `.env` и предоставляет конфигурационные поля.
- `resolved_project_root`, `resolved_mock_hris_path`, `resolved_content_path`, `resolved_assets_dir`, `resolved_docs_dir`, `resolved_rag_index_path`, `resolved_sqlite_db_path` — вычисляемые пути.
- `MockEmbeddings.create()` — возвращает одинаковые тестовые embeddings.
- `MockChatCompletions.create()` — возвращает тестовый JSON sentiment или mock-ответ на вопрос.
- `MockChat`, `MockAsyncOpenAI` — минимальный mock интерфейс, совместимый с местами вызова OpenAI SDK.
- `get_llm_client()` — выбирает реальный `AsyncOpenAI` при непустом действительном ключе, иначе mock-клиент.

### `monitor/bot/max_adapter.py`

- `IncomingMessage`, `IncomingCallback` — внутренние DTO событий; бизнес-логика не использует типы `maxapi`.
- `MaxAdapter.__init__()` — проверяет токен и создаёт `Bot`/`Dispatcher`.
- `keyboard()` — превращает пары label/payload в MAX inline keyboard.
- `send()` — отправляет пользователю текст и, если есть, callback-клавиатуру.
- `send_hr_notice()`, `send_head_hr_notice()` — доставка в настроенные HR/head-HR чаты; при отсутствии адресата записывают предупреждение вместо отправки.
- `bind()` — регистрирует внутренние callback-функции `_start`, `_food`, `_hr`, `_close`, `_simulate`, `_approve`, `_export`, `_text`, `_callback`, `_bot_started`. Callback ACK вызывается в `finally`.
- `_message()` — преобразует `MessageCreated` в `IncomingMessage`.
- `run()` — запускает `Dispatcher.start_polling(bot)`.
- `close()` — закрывает MAX API session.

### `monitor/flows.py`

Файл является основным координатором диалогов. Он также содержит прямые SQL-запросы, поэтому не вся логика БД вынесена в `storage.py`.

- `_load_yaml(path)` — загружает YAML mapping; при отсутствующем/неверном файле явно выдаёт ошибку.
- `_mask_text(text)` — маскирует email, телефон и длинные числовые идентификаторы в части сохраняемого/передаваемого текста.
- `KorsikFlows.__init__(adapter)` — загружает сообщения и опросы; создаёт timeline и RAG engine.
- `_message(key, employee, **values)` — находит шаблон `section.name` и форматирует его.
- `_format_message(template, employee, **values)` — выбирает форму `ty`/`vy`, заполняет имя, площадку и дату выхода.
- `_send(...)` — отправляет сообщение через адаптер и пишет исходящее в `messages_log`.
- `start(event)` — возвращает меню связанному пользователю либо запускает ввод offer-кода.
- `message(event)` — логирует текст, маршрутизирует команды, FSM, handoff и свободный вопрос.
- `callback(event)` — маршрутизирует callback регистрации, опроса, меню и сценарных этапов.
- `bot_started(event)` — проксирует startup MAX в `start()`.
- `send_stage(job, stage_key)` — формирует сообщение для этапа таймлайна; проверяет площадку/категорию; создаёт состояния самопредставления и manager action; отправляет TODO-вложения и опросы.
- `_check_binding_code(user_id, code)` — ищет сотрудника по offer-коду и просит подтверждение.
- `_registration_callback(user_id, payload)` — подтверждает/меняет профиль, сохраняет форму общения и запускает таймлайн.
- `_handoff(...)` — включает режим передачи HR, создаёт ticket и посылает уведомление.
- `_close_handoff(user_id, text)` — проверяет admin ID, закрывает открытый тикет и возвращает пользователя в бот.
- `_answer_free_text(...)` — фильтрует чувствительные темы, вызывает RAG fallback и сохраняет AI request.
- `_rag_stub(...)` — при отключённом RAG не даёт ответ; при включении проверяет эскалацию, порог confidence и наличие цитируемого источника.
- `_log_ai_request(...)` — записывает запрос, решение, причину эскалации и маскированный текст в SQLite.
- `_start_survey(...)` — создаёт сессию опроса и инициализирует FSM.
- `_send_survey_question(...)` — отправляет очередной вопрос с нужным типом клавиатуры или просит свободный текст.
- `_survey_callback(...)` — валидирует сессию/индекс callback, обрабатывает варианты, multi-select и низкие оценки.
- `_score(question, answer, index)` — превращает scale/yes-no/choice в проценты и флаг low.
- `_save_answer(...)` — сохраняет ответ, маскирует комментарий, вызывает анализ текста и специальные red-flag правила.
- `_save_survey_text(...)` — записывает свободный ответ на текстовый вопрос.
- `_save_survey_reason(...)` — обрабатывает причину низкой оценки и продолжает survey.
- `_complete_survey(...)` — ставит статус completed, вычисляет агрегаты, завершает FSM и показывает итог.
- `_analyze_text(...)` — эвристически ищет риск увольнения, токсичность и перегрузку.
- `_create_red_flag(...)` — записывает флаг и уведомляет HR; может создать manager action.
- `_add_manager_action(...)` — записывает запланированную встречу и отправляет заглушку уведомления менеджеру.
- `_schedule_transport_reminder(...)` — добавляет не более двух напоминаний через час.
- `run_survey_reminders()` — проверяет открытые survey и шлёт ограниченные напоминания.
- `run_hr_sla_reminders()` — проверяет открытые тикеты и шлёт напоминание при превышении SLA.
- `send_weekly_digest()` — агрегирует данные SQLite за 7 дней и отправляет дайджест head HR.
- `_simulate(...)` — проверяет администратора и запускает все этапы заданного номера.
- `_approve_stage(...)` — проверяет администратора, сохраняет recruiter approval и возвращает соответствующее job в pending.
- `_export_analytics(...)` — проверяет администратора и запускает CSV-экспорт.
- `_document_list(site)` — строит список документов для площадки.
- `_menu()` — задаёт callback-меню.

### `monitor/storage.py`, `monitor/db.py`, `db/schema.sql`

- `storage.get_employee(user_id)` — поиск связанного профиля по MAX ID.
- `storage.get_employee_by_code(code)` — поиск по `employee_code`, без учёта регистра.
- `storage.link_employee(employee_id, user_id)` — связывает MAX ID с сотрудником и переводит onboarding state.
- `storage.get_state(user_id)`, `storage.set_state(...)` — чтение/запись persistent FSM.
- `storage.log_message(...)` — запись входящих/исходящих сообщений в `messages_log`.
- `storage.create_ticket(...)`, `storage.close_open_ticket(...)` — создание и закрытие тикетов; создание включает `handoff_active`.
- `db._db_path()` — возвращает настроенный SQLite путь.
- `db.init_db()` — запускает схему, миграции legacy таблиц, unique indexes и seed/upsert из mock HRIS.
- `db.log_interaction(...)` — запись в старую таблицу `interactions`; не вызывается активным `flows.py`.
- `db/schema.sql` — определяет employees, таймлайн, состояния, survey, tickets, AI requests, red flags, manager actions и KB-таблицы.

### `monitor/timeline.py`

- `STAGES` — таблица из 25 сценарных точек, stage number, смещение в днях, час и тип сообщения.
- `_shift_to_workday(value)` — переносит субботу/воскресенье на понедельник.
- `TimelineScheduler.__init__(send_stage)` — сохраняет callback отправки и часовой пояс.
- `create_jobs(employee)` — масштабирует даты относительно hire date и вставляет уникальные jobs.
- `run_due()` — выбирает и доставляет готовые jobs; проверяет активность, handoff и recruiter approval.
- `simulate(employee, stage_no)` — сразу отправляет этапы указанного номера без изменения статусов расписания.
- `_set_status(job_id, status)`, `_increment_attempt(job_id)`, `_mark_sent(job_id)` — меняют статус и счётчик попыток.

### `monitor/analytics.py`

- `_week(value)` — преобразует дату события в дату начала недели.
- `_mask(value, full_name)` — маскирует части ФИО, email, телефон и числовой идентификатор.
- `_fetch(db, sql)` — возвращает строки SQLite в виде dict.
- `_write_csv(path, rows, columns)` — записывает CSV с UTF-8 BOM.
- `export_analytics(output_dir)` — извлекает 8 таблиц аналитики, формирует event date/week, маскирует тексты, сохраняет pseudonymous employee IDs и возвращает пути к файлам.

### Остальные Python-модули

- `monitor/rag/engine.py`: `RAGEngine.__init__()` настраивает индекс и LLM клиент; `ensure_loaded()` читает/строит индекс; `answer_question()` выполняет embedding search, confidence gate и grounded answer/эскалацию. Сейчас вызывается только через `_rag_stub`, когда включён `ENABLE_RAG`.
- `monitor/rag/indexer.py`: `DocumentIndexer.__init__()` задаёт каталог и индекс; `_extract_chunks_from_pdf()` режет PDF на фрагменты; `rebuild_index()` читает PDF/TXT, добавляет embeddings и сохраняет JSON; `_get_default_polyplastic_pvtr_chunks()` даёт демонстрационные выдержки при отсутствии документов.
- `monitor/agents/nlu.py`: `SentimentNLUAgent.__init__()` создаёт клиент; `analyze_message()` получает structured sentiment JSON и fallback по ключевым словам. В текущем основном потоке не используется.
- `monitor/agents/summarizer.py`: `DigestSummarizer.__init__()` создаёт клиент; `build_weekly_hr_digest()` собирает старые `interactions` и просит LLM построить обзор. `main.py` вместо него вызывает отдельную SQL-агрегацию в `KorsikFlows.send_weekly_digest()`.
- `monitor/alerts/alert_agent.py`: `AlertEscalationAgent.__init__()` создаёт старый HTTP messenger; `escalate_to_hr()` формирует alert с кнопками; `notify_department_manager()` отправляет manager reminder. К активному потоку не подключён.
- `monitor/bot/delivery.py`: `MaxMessengerClient.__init__()` готовит legacy HTTP client; `send_message()` отправляет JSON через `httpx`; `default_hybrid_menu()` возвращает старые FAQ-кнопки. Не используется `main.py`.
- `monitor/pipeline/scheduler.py`: `OnboardingScheduler.__init__()` создаёт старые delivery/alert агенты; `process_daily_timeline()` обходит старые `onboarding_templates` и `sent_triggers`. Не используется активным `main.py`.
- `monitor/scripts/seed_demo.py`, `monitor/scripts/import_excel.py`: вспомогательные скрипты вне пути запуска приложения.
- `tests/test_demo_flows.py`: `FakeAdapter` сохраняет отправки и HR-notices; `DemoFlowTests.asyncSetUp/asyncTearDown` создают временную БД; `_register()` прогоняет регистрацию; тесты проверяют регистрацию+25 jobs, survey completion, HR handoff и восемь pseudonymous CSV.

## Паттерны и существенные зависимости порядка

- **Adapter:** `KorsikFlows` знает только `MaxAdapter` интерфейс и внутренние DTO; модели MAX изолированы в `monitor/bot/max_adapter.py`.
- **Persistent FSM:** шаг диалога хранится в SQLite; повторный запуск процесса сохраняет состояние.
- **Callback-driven routing:** стабильные строки payload передаются между YAML-кнопками/клавиатурой и `flows.callback()`.
- **Database-backed scheduler:** задания хранятся в SQLite, но опрашиваются встроенным APScheduler процессом; отдельного брокера/очереди нет.
- **Инициализация до polling:** сначала `init_db`, потом конструкторы/регистрация callback handlers, запуск APScheduler, затем `run()`.

## Источники

- `main.py`, `monitor/config.py`
- `monitor/bot/max_adapter.py`, `monitor/flows.py`
- `monitor/storage.py`, `monitor/db.py`, `db/schema.sql`
- `monitor/timeline.py`, `monitor/analytics.py`
- `content/messages.yaml`, `content/surveys.yaml`, `mock_hris.json`
- `tests/test_demo_flows.py`
