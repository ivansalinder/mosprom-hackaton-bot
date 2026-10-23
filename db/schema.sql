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