# Соглашения по коду

## Именование

| Элемент | Соглашение | Пример |
|---|---|---|
| Python-файлы и папки | `snake_case` | `max_adapter.py`, `monitor/rag/` |
| Функции и методы | `snake_case` | `get_employee_by_code()` |
| Внутренние методы | Начинаются с `_` | `_check_binding_code()` |
| Классы | `PascalCase` | `KorsikFlows`, `TimelineScheduler` |
| Константы | `UPPER_SNAKE_CASE` | `TIME_SCALE`, `STAGES` |
| MAX callback payload | Строки с двоеточиями и префиксом по сценарию | `reg:confirm`, `survey:answer:...` |

## Форматирование, импорты и типы

- Язык Python `>=3.11`; типы параметров и возвращаемых значений применяются часто, но не повсеместно.
- Используются стандартные абсолютные импорты вида `from monitor.storage import ...`.
- Публичных barrel-export, alias и специальной конфигурации type checker не обнаружено.
- Конфигурация formatter/linter отсутствует; стиль ориентируется на обычный Python, с четырьмя пробелами отступа.
- Рекомендуется не смешивать детали `maxapi` с бизнес-сценарием: держать их внутри `monitor/bot/max_adapter.py`.

## Ошибки и логи

- Стартовый лог и ошибки расписания используют стандартный `logging`.
- Ошибки timeline отправки логируются с `employee_id` и `stage_key`, попытка увеличивается.
- Отсутствующие content-файлы и неверный YAML приводят к явным исключениям в `_load_yaml()`.
- `max_adapter.send_hr_notice()` при не заданном чате не отправляет сообщение наружу и пишет warning.
- Legacy `SentimentNLUAgent.analyze_message()` ловит общий `Exception` и использует fallback; метод в активном пути не применяется.

## Конфиденциальные данные

- MAX/LLM токены ожидаются в `.env`, не в исходном коде.
- `analytics._mask()` пытается удалить имена, телефоны, email и номера документов из выгрузок.
- `flows._mask_text()` маскирует некоторые идентификаторы при передаче HR/сохранении ответов.
- `storage.log_message()` сохраняет входной текст без маскирования; не отправляйте реальные персональные данные в демо-бот без отдельного решения по защите и хранению.

## Тесты

- Тесты располагаются в `tests/`, файл `test_*.py`, используют `unittest.IsolatedAsyncioTestCase`.
- MAX adapter заменяется `FakeAdapter`; отдельная временная SQLite-БД на каждый тест.
- Проверки включают регистрацию, запись таймлайна, завершение survey, handoff и pseudonymous export.
- Покрытие кода и порог coverage не настроены.

## Источники

- `pyproject.toml`, `.env.example`
- `main.py`, `monitor/config.py`
- `monitor/flows.py`, `monitor/storage.py`, `monitor/bot/max_adapter.py`
- `monitor/analytics.py`, `tests/test_demo_flows.py`
