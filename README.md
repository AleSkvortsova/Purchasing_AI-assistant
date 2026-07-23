# Procurement Intake Assistant

Backend-сервис будущего ИИ-ассистента по внутренним заявкам на закупку.
Приложение предоставляет служебные endpoints и минимальный API черновиков,
который может работать с Supabase PostgreSQL.

## Текущий статус

Реализованы FastAPI-сервис версии `0.1.0`, конфигурация из окружения,
стандартное логирование, SQL-схема Supabase, repository/service-слои и операции
создания, чтения и частичного обновления черновика. База знаний отделена от
проектной документации; реализованы локальная валидация и Markdown-aware
чанкинг.

Telegram, OpenAI API, pgvector, RAG, n8n, Docker Compose, подтверждение и
регистрация заявки на этом этапе не подключены. Чанки пока не содержат
embeddings и не загружаются в Supabase.

## Границы MVP

Утверждённые продуктовые границы и критерии готовности описаны в
[MVP_SCOPE.md](MVP_SCOPE.md). Этот репозиторий пока содержит только технический
фундамент для последующих этапов MVP.

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
3. В Project Settings найдите URL проекта и backend service role key.
4. Создайте локальный `.env`:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-backend-only-service-role-key
DATABASE_URL=
```

`DATABASE_URL` пока необязателен и зарезервирован для будущего прямого
подключения PostgreSQL. Service role key используется только backend-кодом:
его нельзя передавать браузеру, Telegram-клиенту или коммитить.

## Локальный запуск

```powershell
uvicorn app.main:app --reload
```

Сервис будет доступен по адресу `http://127.0.0.1:8000`.

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

Скрипт только подготавливает локальный JSON. OpenAI API, embeddings, поиск и
RAG-ответы ещё не реализованы.

## Проверка Ruff

```powershell
ruff check .
```

## Структура проекта

```text
app/
├── api/             # HTTP routers и dependency injection
├── core/            # настройки и логирование
├── bot/             # будущая Telegram-интеграция
├── llm/             # будущая LLM-интеграция
├── rag/             # будущий поиск по базе знаний
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

Пока не реализованы регистрация и подтверждение заявки, генерация
`request_number`, запись состояния диалога и технических логов, Telegram,
LLM/RAG, embeddings, индексация чанков и авторизация конечных пользователей.
Перед реальным пилотом необходимо включить RLS и определить политики
минимальных прав.

Следующий этап выбирается отдельно и должен оставаться в рамках
`MVP_SCOPE.md`.
