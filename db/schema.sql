-- Сотрудники на онбординге
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    max_user_id TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    role_type TEXT DEFAULT 'new_hire', -- new_hire, hr, manager, head_hr
    plant_site TEXT NOT NULL,          -- Производственная площадка (Москва, Очаково, филиалы)
    department TEXT,
    manager_max_id TEXT,
    hire_date DATE NOT NULL,           -- Дата выхода (для расчета таймлайна)
    start_date DATE,
    is_office BOOLEAN DEFAULT 1,
    onboarding_stage TEXT DEFAULT 'offer', -- offer, day_1, week_1, month_1, month_2, probation_end
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Сценарии проактивных рассылок (импортируются из ТЗ Корсика)
CREATE TABLE IF NOT EXISTS onboarding_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_offset INTEGER NOT NULL,       -- 0 (день 1), 7 (неделя 1), 30, 60, 90 и т.д.
    plant_site TEXT DEFAULT 'ALL',     -- ALL или конкретный завод
    title TEXT NOT NULL,
    message_text TEXT NOT NULL,
    attachment_path TEXT,              -- Путь к PDF-карте или памятке
    is_pulse_survey BOOLEAN DEFAULT 0, -- Является ли сообщение пульс-опросом
    buttons_json TEXT                  -- JSON с кнопками для мессенджера МАКС
);

-- Отправленные триггеры (чтобы не дублировать рассылки по CRON)
CREATE TABLE IF NOT EXISTS sent_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(employee_id, template_id)
);

-- Логи вопросов и результаты Sentiment Analysis (пульс-опросы и свободный диалог)
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    max_user_id TEXT NOT NULL,
    interaction_type TEXT NOT NULL,    -- question, pulse_answer, escalation
    user_text TEXT NOT NULL,
    bot_response TEXT,
    topic_tag TEXT,                    -- доступы, документы, график, отпуск, наставник, токсичность
    sentiment TEXT,                    -- positive, neutral, negative, critical
    risk_score REAL DEFAULT 0.0,
    is_red_flag BOOLEAN DEFAULT 0,
    escalated_to_hr BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_states (
    max_user_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS timeline_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    stage_key TEXT NOT NULL,
    stage_no INTEGER NOT NULL,
    stage_name TEXT NOT NULL,
    message_type TEXT NOT NULL,
    planned_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(employee_id, stage_key)
);

CREATE TABLE IF NOT EXISTS recruiter_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    stage_key TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(employee_id, stage_key)
);

CREATE TABLE IF NOT EXISTS messages_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    direction TEXT NOT NULL,
    stage_key TEXT,
    message_type TEXT NOT NULL,
    text TEXT NOT NULL,
    payload TEXT,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS survey_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    survey_point TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_progress'
);

CREATE TABLE IF NOT EXISTS survey_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    dimension TEXT,
    answer_type TEXT NOT NULL,
    answer_value TEXT,
    score_10 REAL,
    score_pct REAL,
    is_low INTEGER NOT NULL DEFAULT 0,
    comment_text TEXT,
    answered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    context TEXT,
    hr_manager TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    first_response_at TEXT,
    resolved_at TEXT,
    sla_reminded_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    returned_to_bot INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    asked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    topic TEXT,
    channel TEXT NOT NULL DEFAULT 'MAX',
    question_text TEXT NOT NULL,
    retrieval_confidence REAL,
    source_doc TEXT,
    resolution TEXT NOT NULL,
    resolved_by_ai INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    guardrail_blocked INTEGER NOT NULL DEFAULT 0,
    user_feedback TEXT,
    is_knowledge_gap INTEGER NOT NULL DEFAULT 0,
    response_sec REAL
);

CREATE TABLE IF NOT EXISTS red_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    survey_session_id INTEGER,
    source_point TEXT,
    flag_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    quote TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    alert_hr_at TEXT,
    manager_push_at TEXT,
    hr_first_action_at TEXT,
    hr_manager TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS manager_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    manager_id TEXT,
    action_type TEXT NOT NULL,
    sent_at TEXT,
    due_at TEXT,
    acknowledged_at TEXT,
    done_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    file TEXT NOT NULL,
    site_scope TEXT,
    category_scope TEXT,
    version TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding TEXT,
    site_scope TEXT,
    category_scope TEXT
);