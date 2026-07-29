# Procurement Intake Assistant

Backend-сервис будущего ИИ-ассистента по внутренним заявкам на закупку.
Приложение предоставляет служебные endpoints и минимальный API черновиков,
который может работать с Supabase PostgreSQL.

## Текущий статус

Backend MVP этапа intake и request lifecycle завершён и проверен через Swagger
на реальной Supabase. Реализованы сохранение активного черновика,
последовательный сбор и проверка полей, карточка, расчёт маршрута, возврат к
редактированию, регистрация с номером, отмена, optimistic locking,
идемпотентность команд и синхронизация canonical draft с legacy-проекциями.

Также работают OpenAI extraction layer, embeddings, хранение чанков в
Supabase/pgvector, идемпотентная индексация и hybrid retrieval через CLI и API.
Это завершение backend milestone, а не всего продукта: Telegram adapter
поддерживает intake, карточку и lifecycle-действия; production E2E через Telegram,
интерфейс закупщика и фактическое исполнение согласований ещё не реализованы.

## Границы MVP

Утверждённые продуктовые границы и критерии готовности описаны в
[MVP_SCOPE.md](MVP_SCOPE.md). Следующий незавершённый этап — production-ready
пилот Telegram с transport authentication и эксплуатационными настройками.

## Архитектура backend MVP

- FastAPI предоставляет HTTP API, Swagger и dependency injection;
- Supabase PostgreSQL хранит пользователей, заявки, dialog state, команды и
  технические логи;
- pgvector и Russian FTS обеспечивают semantic, lexical и hybrid retrieval;
- Pydantic-модели фиксируют API и внутренние структурированные контракты;
- deterministic intake core отвечает за merge, validation, completeness,
  следующий вопрос и карточку;
- approval rule engine рассчитывает предварительный маршрут;
- persistence orchestration атомарно сохраняет многошаговый intake;
- request lifecycle реализует editing, confirm/register, cancel, optimistic
  locking и idempotency;
- OpenAI extraction layer извлекает факты, но итоговые статусы и правила
  определяются детерминированным кодом;
- Telegram adapter служит тонким transport-слоем поверх backend.

## Требования

- Python `>=3.11,<3.13`;
- Git.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Скопируйте `.env.example` в `.env`, только если нужно переопределить настройки.
Файл `.env` не должен попадать в Git.

## Настройка Supabase

1. Создайте проект в Supabase Dashboard.
2. Откройте SQL Editor и выполните целиком файл
   `scripts/sql/001_initial_schema.sql`.
3. Затем выполните `scripts/sql/002_knowledge_base_vector.sql`.
4. После неё выполните `scripts/sql/003_hybrid_knowledge_search.sql`.
5. Для устойчивого поиска запросов с числами выполните
   `scripts/sql/004_improve_lexical_retrieval.sql`.
6. Затем выполните `scripts/sql/005_approval_rule_engine.sql`.
7. Выполните идемпотентную миграцию
   `scripts/sql/006_fix_approval_rule_ranges.sql`. Для чистого
   развёртывания с исправленной 005 она безопасно повторно закрепит
   те же коды и границы.
8. Выполните `scripts/sql/007_intake_persistence_orchestration.sql`.
9. Выполните `scripts/sql/008_request_lifecycle.sql` после read-only preflight.
10. Выполните repeat-safe
    `scripts/sql/009_sync_request_data_projections.sql`.
11. В Project Settings найдите URL проекта и backend service role key.
12. Создайте локальный `.env`:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-backend-only-service-role-key
TELEGRAM_BOT_TOKEN=your-bot-token
DATABASE_URL=
OPENAI_API_KEY=your-openai-api-key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_BATCH_SIZE=50
RAG_TOP_K=5
RAG_SIMILARITY_THRESHOLD=0.0
RAG_RETRIEVAL_MODE=hybrid
RAG_SEMANTIC_CANDIDATE_COUNT=20
RAG_LEXICAL_CANDIDATE_COUNT=20
RAG_RRF_K=60
RAG_SEMANTIC_WEIGHT=1.0
RAG_LEXICAL_WEIGHT=1.0
ENABLE_RAG_INDEX_ENDPOINT=false
```

`DATABASE_URL` пока необязателен и зарезервирован для будущего прямого
подключения PostgreSQL. Service role key используется только backend-кодом:
его нельзя передавать браузеру, Telegram-клиенту или коммитить.

## Локальный запуск

```powershell
uvicorn app.main:app --reload
```

Сервис будет доступен по адресу `http://127.0.0.1:8000`.

Минимальный Telegram adapter запускается отдельно через long polling:

```powershell
python -m app.bot
```

Для него нужны `TELEGRAM_BOT_TOKEN` и серверная конфигурация Supabase. Токен
не требуется при импорте модулей или запуске тестов. При наличии
`OPENAI_API_KEY` Telegram по умолчанию работает в `hybrid`, без ключа — в
`rule`. В OpenAI-first `hybrid` structured provider отвечает за смысловые поля,
а deterministic parser — за точные
числа, даты, явный бюджетный статус и консервативный fallback. Явная настройка —
`TELEGRAM_EXTRACTION_MODE=rule|openai|hybrid`. Во всех режимах показываются
только «Товар» и «Услуга», а validators и intake core остаются источником
истины. Подробнее:
[`docs/telegram_adapter.md`](docs/telegram_adapter.md).

## Проверка health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

Оба запроса возвращают:

```json
{"status": "ok"}
```

Проверка подключения к Supabase:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/db/health
```

Без переменных Supabase endpoint вернёт контролируемый статус
`not_configured`, при успешном соединении — `ok`.

## API черновиков

После запуска откройте Swagger UI: `http://127.0.0.1:8000/docs`.

Доступные операции:

- `POST /api/v1/requests` — создать черновик;
- `GET /api/v1/requests/{request_id}` — прочитать заявку;
- `PATCH /api/v1/requests/{request_id}` — частично обновить черновик.

Пример тела `POST`:

```json
{
  "user_id": "11111111-1111-4111-8111-111111111111",
  "request_type": "product",
  "category_code": "G02",
  "title": "Мониторы для отдела продаж",
  "data": {
    "quantity": 10,
    "unit": "шт."
  }
}
```

Перед созданием заявки соответствующий пользователь должен существовать в
таблице `users`. PATCH объединяет переданные поля `data` с уже сохранёнными
данными. Изменение заявки со статусом, отличным от `draft`, запрещено.

## Запуск тестов

```powershell
pytest
```

## Подготовка базы знаний

Источники внутренних правил находятся в `knowledge_base/`, а проектная
документация — в `docs/`. Для локальной проверки метаданных и создания
детерминированных чанков выполните:

```powershell
python scripts/prepare_knowledge_base.py
```

Manifest сохраняется в `knowledge_base/manifest.json`, документы, чанки,
validation report и статистика — в `data/processed/`. Подробности описаны в
[`docs/knowledge_base_preparation.md`](docs/knowledge_base_preparation.md).

Безопасная проверка подготовленных данных не создаёт внешние клиенты, не
обращается к сети и ничего не записывает:

```powershell
python scripts/index_knowledge_base.py --dry-run
```

Реальная индексация ниже вызывает платный OpenAI API и записывает данные в
Supabase, поэтому запускайте её только явно:

```powershell
python scripts/index_knowledge_base.py
python scripts/search_knowledge_base.py `
  "Кто согласует закупку на 180000 рублей?" `
  --mode hybrid `
  --top-k 5 `
  --debug-scores
```

API предоставляет `GET /api/v1/rag/health`, `POST /api/v1/rag/search` и
отключённый по умолчанию `POST /api/v1/rag/index`. Поиск возвращает
релевантные чанки без генерации ответа. По умолчанию используется `hybrid`;
режимы `semantic` и `lexical` можно передать в API или CLI.

Сравнение retrieval на 15 контрольных вопросах:

```powershell
python scripts/evaluate_retrieval.py `
  --mode all `
  --top-k 5 `
  --show-failures
```

Вариант `--offline` не использует сеть и проверяет только архитектуру и расчёт
метрик. Подробности: [`docs/rag_indexing.md`](docs/rag_indexing.md) и
[`docs/hybrid_retrieval.md`](docs/hybrid_retrieval.md).

## Предварительный маршрут согласования

Rule engine принимает только структурированные параметры и не использует
OpenAI или RAG:

```powershell
python scripts/validate_approval_rules.py
python scripts/evaluate_approval_route.py `
  --amount 180000 `
  --budget-status budgeted
```

API: `POST /api/v1/approval-rules/evaluate` и
`GET /api/v1/approval-rules/health`. Результат является предварительным
маршрутом, а не фактическим согласованием. Подробности:
[`docs/approval_rule_engine.md`](docs/approval_rule_engine.md).

## Извлечение контекста согласования

Extraction layer отделён от rule engine: provider извлекает только факты и
evidence, детерминированный код нормализует значения, после чего orchestrator
может передать готовый `ApprovalContext` в `ApprovalRuleService`.

Локальный режим не использует сеть:

```powershell
python scripts/extract_approval_context.py `
  "Юридические услуги на 600 тысяч, закупка бюджетная" `
  --provider rule-based `
  --json

python scripts/evaluate_approval_extraction.py --offline --show-failures

python scripts/validate_approval_extraction_schema.py
```

API: `GET /api/v1/approval-context/health`,
`POST /api/v1/approval-context/extract` и
`POST /api/v1/approval-context/extract-and-evaluate`.
OpenAI provider использует Structured Outputs и включается явно; отсутствие
ключа не приводит к скрытому fallback. Для безопасной технической диагностики
CLI поддерживает `--debug`; без него
сообщение об ошибке остаётся кратким. Подробности:
[`docs/approval_context_extraction.md`](docs/approval_context_extraction.md).

## Детерминированный intake-диалог

Пакет `app/intake` реализует локальное пошаговое заполнение черновика: реестр
полей, безопасный merge, валидацию, полноту, один следующий вопрос, интеграцию
с существующим rule engine и структурированную карточку. Технические endpoints
`GET /api/v1/intake/health` и `POST /api/v1/intake/evaluate-step` ничего не
сохраняют и не вызывают OpenAI. Подробности и ограничения описаны в
[`docs/intake_dialog_core.md`](docs/intake_dialog_core.md).

Persistence orchestration восстанавливает многошаговый draft, сохраняет
dialog state и безопасные message logs, поддерживает idempotency и optimistic
locking. Migration 007 применена и проверена в Supabase. Технический `user_id`
persistence API
пока приходит от клиента и не подтверждён transport authentication, поэтому
endpoint нельзя открывать как production API до привязки authenticated
identity. Подробнее:
[`docs/intake_persistence_orchestration.md`](docs/intake_persistence_orchestration.md)
и [`docs/intake_persistence_migration_runbook.md`](docs/intake_persistence_migration_runbook.md).

После `ready_for_confirmation` отдельный детерминированный lifecycle-слой
показывает актуальную карточку, возвращает draft к редактированию, отменяет или
атомарно регистрирует его со статусом `new` и номером `PR-YYYY-NNNNNN`.
Migration 008 применена и проверена; migration 009 синхронизирует проекции из
`data.intake.draft`, защищает registration snapshot и repeat-safe применена
дважды. Offline demo запускается командой
`python scripts/demo_request_lifecycle.py`.
Подробности: [`docs/request_lifecycle.md`](docs/request_lifecycle.md).

Допустимые persistence statuses текущего MVP: `draft`, `new`, `cancelled`.
Отдельный `data.intake.intake_status` описывает состояние диалога:
`collecting`, `ready_for_confirmation`, `editing`, `completed`, `cancelled`
(а при противоречиях также технический `conflict`). `requests.status` отвечает
за жизненный цикл записи, intake status — за состояние сбора данных.

## Проверка Ruff

```powershell
ruff check .
```

## Структура проекта

```text
app/
├── api/             # HTTP routers и dependency injection
├── core/            # настройки и логирование
├── extraction/      # structured extraction, normalization и orchestration
├── bot/             # тонкий Telegram adapter и long-polling entrypoint
├── llm/             # будущая LLM-интеграция
├── intake/          # детерминированное ядро intake-диалога
├── rag/             # embeddings, индексация, FTS и hybrid retrieval
├── rules/           # детерминированный расчёт маршрута согласования
├── repositories/    # Supabase и InMemory хранилища
├── schemas/         # Pydantic-схемы API
├── services/        # операции с черновиками и DB health
└── main.py          # точка входа FastAPI
docs/                # проектная документация
knowledge_base/      # Markdown-источники внутренних правил
prompts/             # будущие шаблоны промптов
scripts/             # служебные скрипты
└── sql/             # SQL-миграции Supabase
data/processed/      # локальный результат подготовки базы знаний
tests/               # автоматические тесты
MVP_SCOPE.md         # утверждённые границы MVP
pyproject.toml       # зависимости и настройки инструментов
```

## Следующие этапы

Реализован **Telegram adapter MVP**: long-polling transport, `/start`, главное
меню, привязка `telegram_id`, OpenAI-first extraction с консервативным
deterministic fallback,
контекстные вопросы,
карточка заявки и lifecycle-кнопки confirm/edit/cancel поверх существующих
backend services. Полный production E2E, интерфейс
закупщика, фактическое исполнение согласований, production deployment,
генерация RAG-ответа и reranker пока не реализованы.
Перед реальным пилотом необходимо включить RLS и определить политики
минимальных прав.
