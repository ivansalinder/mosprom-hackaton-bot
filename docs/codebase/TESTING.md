# Тестирование

## Инструменты и команды

- Тестовый runner: стандартный `unittest` из Python.
- Async-тесты: `unittest.IsolatedAsyncioTestCase`.
- MAX подменяется локальным `FakeAdapter`; база изолируется временным файлом.
- Проверенные команды:

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q main.py monitor tests
uv lock --check
```

## Набор проверок

Файл `tests/test_demo_flows.py` содержит:

| Проверка | Что подтверждает |
|---|---|
| `test_registration_creates_persistent_timeline` | `/start` → offer code → подтверждение → форма обращения; в БД регистрируется GTP-профиль и создаются 25 таймлайн-заданий |
| `test_day_three_survey_persists_and_completes` | Ответы на три вопроса опроса Дня 3 записываются, сессия становится completed, FSM очищается |
| `test_hr_command_enters_handoff` | `/HR` создаёт открытый тикет и устанавливает `handoff_active` |
| `test_export_creates_eight_pseudonymous_files` | создаются восемь CSV; экспорт включает псевдоним сотрудника и не включает ФИО или MAX user ID |

`DemoFlowTests.asyncSetUp()` создаёт TemporaryDirectory, временную БД, запускает `init_db()` и создаёт `KorsikFlows`. `_register()` выполняет регистрацию через FakeAdapter; `asyncTearDown()` восстанавливает исходный DB path и очищает временную папку. Сам FakeAdapter хранит вызовы отправки и HR notifications в списках.

## Границы текущего покрытия

- Это интеграционные проверки business flow + SQLite, но не тесты против реального MAX.
- Нет end-to-end теста с реальными update/callback объектами `maxapi`.
- Не проверяются сетевые сбои и production HR chat delivery.
- Нет отдельного coverage инструмента, измеренного baseline или обязательного порога.
- Не покрыты все stage-ветки, RAG с реальным LLM и все сценарии red flags.

## Источники

- `tests/test_demo_flows.py`
- `monitor/flows.py`, `monitor/storage.py`
- `monitor/analytics.py`, `monitor/db.py`
- `pyproject.toml`
