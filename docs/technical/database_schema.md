# Схема базы данных

Схема предназначена для первого этапа хранения пользователей, черновиков
заявок, состояния диалога и технических сообщений в Supabase PostgreSQL.
Основная миграция находится в `scripts/sql/001_initial_schema.sql`, а расширение
схемы для семантического поиска — в `scripts/sql/002_knowledge_base_vector.sql`.
Full-text и hybrid retrieval добавляет
`scripts/sql/003_hybrid_knowledge_search.sql`. Миграция
`scripts/sql/004_improve_lexical_retrieval.sql` безопасно обновляет только
lexical-функции и права доступа.
Структурированные правила предварительного маршрута создаёт
`scripts/sql/005_approval_rule_engine.sql`. Для баз, где была выполнена
его прежняя версия с целочисленными границами, миграция
`scripts/sql/006_fix_approval_rule_ranges.sql` идемпотентно заменяет
старые коды и уточняет границы до 0.01.
Persistence orchestration добавляет migration 007. Исходная migration 008
расширяет схему атомарным lifecycle регистрации и отмены заявки, сохраняя при
confirm прежний intake JSON и заменяя только lifecycle snapshot. Применённая
следом repeat-safe migration 009 синхронизирует canonical intake draft с
дублирующими проекциями и устанавливает registration trigger. Все три миграции
применены и проверены в Supabase; уже применённые файлы сохраняются неизменными.

## Связи

```text
users 1 ─── * requests
  │               │
  │               └── 0..* message_logs
  ├── 0..1 dialog_states
  └── 0..* message_logs

dialog_states.active_request_id ─── 0..1 requests.id

knowledge_documents 1 ─── * knowledge_chunks

approval_base_rules
approval_additional_rules

requests 1 ─── * request_lifecycle_commands
```

## Таблицы

### `users`

Внутренние пользователи ассистента. `telegram_id` пока необязателен.
Допустимые роли: `requester`, `buyer`, `admin`.

### `requests`

Заявки и черновики. Intake создаёт `draft`; lifecycle после явного
подтверждения переводит его в `new`, присваивает `request_number` и сохраняет
registration timestamps/actor. Отмена до регистрации переводит draft в
`cancelled` без номера. Тип заявки может быть `product`, `service` или `NULL`,
пока черновик не заполнен.

Колонка `request_type` имеет тип `text`, но ограничена CHECK constraint
значениями `product`, `service` и `NULL`. Канонический
`data.intake.draft.procurement_type` использует только `goods` и `service`;
JSONB технически не имеет отдельного SQL constraint. Старое значение `work`,
если оно осталось в JSON, можно посчитать без изменений данных запросом
`scripts/sql/check_legacy_work_records.sql`.

Поле `data` хранит изменяемые категорийные поля заявки в JSONB. Канонический
источник актуальных значений intake — `data.intake.draft`. Верхнеуровневые
колонки и legacy-ключи `data` являются синхронизированными проекциями.
Migration 009 исправляет уже зарегистрированные строки и устанавливает trigger
для последующих регистраций; повторное применение не меняет version,
`updated_at` или lifecycle snapshots. Статусы, разрешённые схемой: `draft`,
`new`, `cancelled`.

| Внутреннее значение | Название для пользователя |
|---|---|
| `draft` | «Черновик» |
| `new` | «Передана в отдел закупок» |
| `cancelled` | «Отменена» |

### `dialog_states`

Текущее состояние диалога. Уникальность `user_id` обеспечивает не более одного
активного состояния на пользователя. Активная заявка необязательна. Поле
`current_intent` хранит persistent-режим Telegram-диалога: `idle`, `intake` или
`regulation_qa`. Переключение режима обновляет только intent и не заменяет
`active_request_id` либо `state_data`, поэтому справочный вопрос не изменяет
черновик. Для этого milestone новая миграция не требуется.

### `message_logs`

Технический журнал intake-сообщений, lifecycle-команд, длительности, источников
и ошибок. Lifecycle events имеют отдельные message types и metadata и не
смешиваются с обычными вопросами intake.

### `request_lifecycle_commands`

Migration 008 хранит результат mutation-команды для namespace
`(user_id, command_type, idempotency_key)`. Это позволяет вернуть replay до
проверки устаревшей версии и не создавать повторный номер или audit logs.
Таблица не заменяет `message_logs`: она хранит технический idempotency result.

### `knowledge_documents`

Метаданные документов подготовленной базы знаний: стабильный `document_id`,
тип, заголовок, версия, приоритет, признак активности, исходный путь и
контрольная сумма содержимого. Исходный Markdown в таблице не дублируется.

### `knowledge_chunks`

Фрагменты документов со стабильным UUID, текстом, заголовками, порядковым
номером и `content_sha256`. Вектор хранится только на уровне чанка:
`embedding vector(1536)` остаётся `NULL` до индексации, а
`embedding_model` фиксирует модель, которой он рассчитан.

Для cosine-поиска создан частичный HNSW-индекс с `vector_cosine_ops` только по
строкам, где embedding уже рассчитан. RPC-функция
`match_knowledge_chunks` вычисляет similarity как
`1 - (embedding <=> query_embedding)`, учитывает только активные документы,
поддерживает фильтр по типам документов и порог similarity. Результаты
сортируются по similarity, приоритету документа и номеру чанка.

Повторная индексация идемпотентна: embedding сохраняется, пока не изменились
`content_sha256` и модель. Устаревшие строки удаляются только по явно
вычисленному списку UUID и только после успешного расчёта и сохранения всех
новых embeddings.

Миграция 003 добавляет generated stored `search_vector`, GIN-индекс,
нормализацию чисел, RPC `match_knowledge_chunks_lexical` и
`match_knowledge_chunks_hybrid`. Предпочтительна встроенная конфигурация FTS
`russian`; при её отсутствии миграция автоматически использует `simple`.
Hybrid score вычисляется через RRF по позициям semantic и lexical кандидатов,
а не сложением raw cosine similarity и `ts_rank`.

### `approval_base_rules`

Базовые диапазоны сумм для `budgeted` и `unbudgeted`. Согласующие хранятся
непустым JSON-массивом строк; каждое правило содержит период действия и ссылку
на первичный документ.

Канонические границы не имеют разрывов: для `budgeted` это
0.00–100000.00, 100000.01–500000.00 и 500000.01+; для `unbudgeted` —
0.00–100000.00 и 100000.01+.

### `approval_additional_rules`

Условия срочности, единственного поставщика, категории, доступа к данным и
работ на объекте. Seed обеих таблиц обновляется идемпотентно по `rule_code`.

## Временные поля

Функция `public.set_updated_at()` и триггеры автоматически обновляют
`updated_at` в `users`, `requests` и `dialog_states`.

## Безопасность

Backend использует `SUPABASE_SERVICE_ROLE_KEY`, который нельзя передавать
клиенту, возвращать через API или записывать в логи. RLS-политики для конечных
пользователей пока не создаются. Перед пилотом необходимо включить RLS,
определить политики и проверить минимально необходимые права.

Таблицы базы знаний и RPC предназначены для вызова только backend-кодом с
`SUPABASE_SERVICE_ROLE_KEY`. Клиентский доступ к ним и ключу service role
запрещён.

Таблицы правил доступны `service_role` только на чтение. Evaluation endpoint
не изменяет заявки и не выполняет согласование.
