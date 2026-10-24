# Журнал работ — Корсик MAX-бот

## ШАГ 0: Подготовка и запуск «как есть»

### Что сделано
- Прочитаны все docs/*.md (ARCHITECTURE, CONCERNS, CONVENTIONS, INTEGRATIONS, STACK, STRUCTURE, TESTING)
- Прочитаны все ключевые файлы кода: main.py, monitor/config.py, monitor/flows.py, monitor/timeline.py, monitor/storage.py, monitor/db.py, monitor/bot/max_adapter.py
- Прочитаны content/messages.yaml, content/surveys.yaml, db/schema.sql, tests/test_demo_flows.py
- Прочитан SCENARIO.md (встроен в инструкцию)

### .env
- Файл .env существует
- Содержит: LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, EMBEDDING_MODEL, MAX_BOT_TOKEN
- Недостающие переменные (нужно добавить в .env и .env.example):
  - ADMIN_USER_IDS (нет в .env, есть в .env.example)
  - HR_ESCALATION_CHAT_ID (нет в .env, есть в .env.example)
  - HEAD_HR_CHAT_ID (нет в .env, есть в .env.example)
  - TIMEZONE (нет в .env, есть в .env.example, default Europe/Moscow)
  - TIME_SCALE (нет в .env, есть в .env.example, default 1)
  - DEMO_MODE (нет в .env, есть в .env.example, default true)
  - HR_SLA_HOURS (нет в .env, есть в .env.example, default 4)
  - RAG_MIN_CONFIDENCE (нет в .env, есть в .env.example, default 0.65)
  - ENABLE_RAG (нет в .env, есть в .env.example, default false)
  - BINDING_METHOD (нет в .env, есть в .env.example, default offer_code)
  - SQLITE_DB_PATH (нет в .env, есть в .env.example, default data/korsik.db)
  - RECRUITER_URL (нет в .env, нет в .env.example — нужно добавить)
  - SHOW_ASSET_STUBS (нет в .env, нет в .env.example — нужно добавить)

### Результаты проверок

compileall: [проверить после запуска]
unittest: [проверить после запуска]
check_scenario_texts: [не создан, пропуск]

### Найденные проблемы
1. `.env` не содержит многих переменных из `.env.example` (но они имеют дефолты в config.py, так что это не критично)
2. `config.py` не содержит новых полей: `RECRUITER_URL`, `SHOW_ASSET_STUBS`, `IT_ESCALATION_CHAT_ID`
3. `mock_hris.json` использует offer-коды типа `DEMO-GRPP-OFFICE`, а не русские слова
4. `content/messages.yaml` содержит устаревшие/короткие тексты, не совпадающие с SCENARIO.md
5. `content/surveys.yaml` имеет упрощённые определения опросов
6. `monitor/timeline.py` STAGES содержит 25 ключей, но SCENARIO.md определяет 23 ключа таймлайна + pre_block
7. `monitor/flows.py` использует старый формат привязки (offer-код из mock HRIS)
8. `monitor/flows.py` `_answer_free_text` использует RAG-путь, а не новый HELP_BUTTONS
9. Нет команды `/stages`, `/next`, `/jump`, `/reset`, `/me`, `/stats` для сотрудников
10. Нет файла `scripts/check_scenario_texts.py`
11. Нет папки `assets/`
12. Нет файла `monitor/stages.py`
13. Нет файла `monitor/assets.py`
