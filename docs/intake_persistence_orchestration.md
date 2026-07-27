# Persistence orchestration intake-диалога

## Назначение и границы

`app/intake_persistence` связывает детерминированный `RequestIntakeService` с
хранилищем. Intake core по-прежнему отвечает только за merge, validation,
completeness, следующий вопрос, approval context и карточку. Orchestrator
выбирает заявку, восстанавливает draft, вызывает core и сохраняет один шаг.
Repositories выполняют только операции хранения. API остаётся тонким.

Слой не вызывает OpenAI, не извлекает произвольные поля из текста, не
регистрирует и не отправляет заявку.

## Результат аудита схемы

Существующие таблицы уже дают основную модель:

- `requests`: пользователь, тип, категория, title, persistence status и
  расширяемый `data jsonb`;
- `dialog_states`: одна строка на пользователя, ссылка на активную заявку и
  `state_data jsonb`;
- `message_logs`: связь с пользователем и заявкой, timestamps и технические
  поля;
- `users`: владелец заявки.

Migration 007 добавила request version, уникальный active draft, уникальный
idempotency namespace и атомарную операцию request + dialog + два лога.
`updated_at` по-прежнему не используется как concurrency token: optimistic
locking основан на явном поле `version`.

## Активный черновик

Persistence-статус остаётся существующим `draft`. Intake-статусы
`collecting`, `conflict` и `ready_for_confirmation` находятся внутри JSON и не
меняют SQL enum. Поэтому готовая к подтверждению карточка остаётся активным
draft до команды lifecycle confirm или cancel.

При явном `request_id` проверяются существование, владелец и редактируемость.
Без ID repository атомарно находит или создаёт draft. Несколько ранее
существующих draft не выбираются молча: возвращается
`MultipleActiveDraftsError`. Prepared partial unique index предотвращает новые
дубликаты после применения migration 007.

Find-or-create остаётся двухшаговой операцией на уровне REST adapter: сначала
выполняется чтение, затем insert. От гонки защищает именно partial unique index,
а не предварительный select. Проигравший insert перехватывает unique violation,
повторно читает активный draft и продолжает работу с ним без ответа 500.

## Формат `requests.data`

```json
{
  "schema_version": 1,
  "intake": {
    "draft": {},
    "field_states": {},
    "conflicts": [],
    "warnings": [],
    "intake_status": "collecting",
    "next_question": null,
    "audit": {}
  }
}
```

Сохраняются нормализованные значения, источник и подтверждение каждого поля,
активные конфликты, warnings, ожидаемый вопрос, intake status и безопасные
audit identifiers. `completeness`, `RequestCard`, `ApprovalContext` и route
пересчитываются при восстановлении: они зависят от актуального registry и
approval rules. Display fallback title не записывается как значение `title`.

Канонический источник значений — нормализованный `RequestDraftData`, сохранённый
как `data.intake.draft` вместе с `field_states`. Верхнеуровневые `data.amount`,
`data.quantity`, `data.unit`, `data.required_date` и остальные legacy-поля —
только совместимая проекция. На каждом успешном intake step mapper полностью
перезаписывает эту проекцию из того же draft, включая `null`, поэтому прежнее
значение не может пережить исправление. Колонки `request_type`, `category_code`
и `title` формируются из того же draft; display fallback по-прежнему не
записывается как `title`.

После появления `data.intake` канонические поля нельзя менять через legacy
`PATCH /requests/{id}`: такой запрос вернёт контролируемый conflict и должен
быть заменён обычным intake step. PATCH несвязанных integration-ключей остаётся
доступным и не меняет draft или его проекции.

Отсутствующий intake JSON у legacy draft интерпретируется как schema version 1
и объединяется с основными колонками. Неизвестная будущая версия отклоняется,
а произвольная повреждённая структура не интерпретируется молча.

Mapping типов централизован:

- `goods ↔ product`;
- `service ↔ service`;
- `work → service`, при этом исходное `work` сохраняется в intake draft и
  восстанавливается из JSON.

## Dialog state

`dialog_states.state_data` содержит `PersistentDialogState`: user/request ID,
intake status, ожидаемое поле, сериализованный вопрос, conflict ID,
`state_version` и metadata. `state_version` равен сохранённой request version.
Для `ready_for_confirmation` ожидаемое поле и вопрос отсутствуют. Несовпадение
user/request или повреждённый JSON возвращает безопасную ошибку.

## Message logs и идемпотентность

Один сохранённый шаг создаёт incoming `structured_update` и outgoing
`question`, `conflict` либо `card`. Payload структурирован и не содержит API
keys, Authorization headers, prompts или traceback.

Idempotency namespace — `(user_id, idempotency_key)`. Хранится SHA-256
fingerprint канонического update и безопасный persisted result. Повтор того же
payload возвращает `replayed=true` без новой версии и логов. Тот же ключ с
другим payload даёт 409. Разные пользователи могут использовать одинаковый
внешний ключ.

InMemory Unit of Work обеспечивает это атомарно. В Supabase строгая защита от
race появляется после применения unique index из migration 007; одна лишь
select-before-insert строгой гарантией не считается.

## Optimistic locking и атомарность

Prepared migration добавляет `requests.version bigint not null default 1`.
RPC обновляет строку только при `version = expected_version`, затем увеличивает
версию. Несовпадение даёт `ConcurrentIntakeUpdateError` и не перезаписывает
чужие данные.

Внутри RPC порядок проверок фиксирован: готовый idempotent replay проверяется
до stale version, затем под блокировкой проверяются владелец и статус, после
чего выполняются versioned update, dialog upsert и обе записи журнала. Все эти
операции входят в одну транзакцию вызова функции. RPC имеет фиксированный
`search_path`, schema-qualified таблицы, владельца `postgres`; EXECUTE отозван
у `PUBLIC`, `anon` и `authenticated` и выдан только `service_role`.

InMemory repository моделирует настоящую транзакцию: request, dialog, incoming
и outgoing logs, а также idempotency record публикуются только вместе. Можно
имитировать сбой на каждом этапе; staged changes полностью отбрасываются.

Supabase adapter использует единственный RPC `save_intake_step`, поэтому
обновление существующего request и сопутствующие записи атомарны после
применения migration 007. Первичное создание пустого request выполняется перед
RPC: при последующем сбое может остаться version 1 draft без применённого шага.
Orchestrator возвращает `partial_failure`, recovery metadata и не сообщает о
полном успехе. Повтор с тем же ключом безопасно применяет несохранённый шаг.
Запись отдельного `system_error` в этой ситуации является только best effort:
если сама БД недоступна, журнал также может не сохраниться. Recovery metadata
не содержит traceback, ключей или других секретов.

Пустой draft сейчас автоматически не удаляется. Безопасный кандидат для
будущей сверки — draft с `version = 1`, без intake payload, dialog state и
успешного incoming idempotency log. Самого возраста или версии недостаточно
для автоматического удаления: retention/reconciliation policy должна быть
согласована владельцем данных отдельно.

Rollback migration 007 должен выполняться отдельным согласованным изменением:
сначала прекратить вызовы RPC, затем удалить функцию и индексы, и только после
проверки потребителей — новые колонки. Автоматического rollback нет.

## API и demo

- `GET /api/v1/intake-sessions/health`;
- `POST /api/v1/intake-sessions/step`;
- `GET /api/v1/intake-sessions/{user_id}/active`;
- `python scripts/demo_persistent_intake.py`.

В текущем техническом MVP `user_id` приходит из body/path и не подтверждается
transport authentication. Поэтому endpoint нельзя считать production-safe:
до внешнего доступа identity должна поступать из проверенного токена/канала, а
не из пользовательского тела запроса. Наружу возвращаются стабильные
403/404/409/422/503 без traceback и внутренних DB details.

Demo использует общий `InMemoryIntakeStorage`, но создаёт новый repository и
orchestrator после каждого шага. Он показывает восстановление, replay,
explicit correction, рост версии, логи и финальную карточку без сети.

## Ограничения до Telegram

- migration 007 применена и проверена в Supabase; preflight и runbook сохранены
  для будущих развёртываний;
- пользователя и канал аутентифицирует будущий transport layer;
- confirm/register, cancel, request number и переход `draft → new` реализованы
  отдельным request lifecycle layer;
- reconciliation worker и operational monitoring partial failures пока не
  реализованы;
- extraction остальных intake-полей и Telegram markup остаются вне слоя.

## Read-only preflight

Перед migration запускается `scripts/sql/007_intake_persistence_preflight.sql`.
Его result sets интерпретируются так:

1. Status counts — контроль исходного объёма; строки допустимы.
2. Duplicate active drafts — должен быть пуст; каждую строку вручную разрешает
   владелец данных, сохраняя нужный request ID.
3. Duplicate idempotency namespaces — должен быть пуст; автоматическое
   объединение запрещено.
4. Orphan dialog states — должен быть пуст; требуется ручная сверка ссылки.
5. Dialog/request user mismatch — должен быть пуст и требует security review.
6. Broken message-log relations — должен быть пуст; строки сохраняются для
   расследования до любого исправления.
7. Required columns — до первого запуска часть строк ожидаемо отсутствует,
   после migration должен присутствовать полный набор.
8. Required indexes — до первого запуска могут отсутствовать, после migration
   должны быть обе строки с partial predicates.
9. RPC — после migration должна быть одна ожидаемая сигнатура, owner
   `postgres`, `security_definer = true`; ACL должен давать EXECUTE только
   `service_role` среди клиентских ролей.

Полная процедура применения и повторной проверки описана в
`docs/intake_persistence_migration_runbook.md`.
