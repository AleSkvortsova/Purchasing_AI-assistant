# Жизненный цикл заявки после intake

## Границы этапа

`app/request_lifecycle` управляет заявкой после завершения детерминированного
intake: показывает актуальную карточку, возвращает её к редактированию,
регистрирует или отменяет черновик. Слой не вызывает OpenAI, не отправляет
уведомления, не выполняет согласование и не разрешает редактирование после
регистрации.

## Две системы статусов

Persistence status в `requests.status`:

- `draft` — сбор данных, редактирование или ожидание подтверждения;
- `new` — зарегистрирована и передана в отдел закупок;
- `cancelled` — черновик отменён пользователем.

Dialog/intake status:

- `collecting`;
- `conflict`;
- `ready_for_confirmation`;
- `editing`;
- `completed`;
- `cancelled`.

Dialog status не записывается в `requests.status`. Существующий SQL constraint
уже допускает ровно `draft/new/cancelled`; миграция его не меняет. Документы
базы знаний используют пользовательские названия «Черновик», «Передана в отдел
закупок» и «Отменена заказчиком» и не противоречат техническим кодам.

## Confirmation view и повторная проверка

`get_confirmation_view` ничего не записывает. Он восстанавливает draft из
`requests.data`, повторно запускает актуальные completeness, card builder,
ApprovalContext и approval rules. Старые display-card, completeness и route не
считаются источником истины.

`confirmable=true` только если request остаётся `draft`, сохранённый intake
ожидает подтверждения, актуальный intake имеет `ready_for_confirmation`,
completeness полный, нет conflicts/invalid fields, сформирована карточка,
ApprovalContext существует и approval route имеет `resolved`. Если readiness
изменилась, регистрация не выполняется, dialog возвращается к
`collecting/conflict`, а API отвечает контролируемым 409 с актуальным view.
Fallback title отображается в карточке, но не записывается как пользовательский
`title`.

Именно Python является владельцем этих бизнес-проверок: registry полей,
completeness, построения карточки, ApprovalContext и approval rules. В RPC
передаётся уже пересчитанный snapshot. SQL не вычисляет готовность повторно и
не интерпретирует содержимое карточки или маршрута; он проверяет idempotency,
ownership, статус и version заявки, допустимый server-owned dialog status и
согласованность request/dialog, после чего атомарно сохраняет результат.

## Регистрация и номер

Подтверждение принимает `expected_version` и выполняется одним RPC. Перед
выдачей номера RPC блокирует request, проверяет владельца, `draft` и версию.
Номер имеет формат `PR-YYYY-NNNNNN`. Числовая часть берётся из глобальной
PostgreSQL sequence, а не из `max()+1`; sequence не сбрасывается ежегодно и
может иметь безопасные пропуски после rollback. Draft и cancelled номера не
получают.

Успешная команда переводит `draft → new`, увеличивает version ровно на 1,
записывает `registered_at`, `confirmed_at`, `confirmed_by`, завершает dialog и
создаёт два lifecycle audit events.

`confirmed_by` и `cancelled_by` используют тот же внутренний UUID
`public.users.id`, что и `requests.user_id`. Внешний `telegram_id` хранится
отдельно и в actor-поля lifecycle не записывается. Migration 008 закрепляет
это внешними ключами на `public.users(id)`.

## Финальный snapshot

В `requests.data.lifecycle` сохраняются:

- `registered_schema_version=1`;
- фактические `registered_at/confirmed_at`;
- `confirmed_by`;
- `final_request_card`;
- `final_approval_route`;
- `final_completeness`;
- `registry_version`;
- `approval_rules_version`.

Snapshot фиксирует подтверждённое пользователем состояние и после регистрации
не редактируется в MVP. Он добавляется без перезаписи intake draft и unrelated
ключей `requests.data`.

## Возврат к редактированию

Команда допустима только для актуальной версии `draft` со статусом
`ready_for_confirmation`. Persistence status остаётся `draft`, номер и snapshot
регистрации не создаются, dialog становится `editing`. Ответ содержит текущую
карточку, коды редактируемых полей и инструкцию отправить обычный structured
update. Intake заново рассчитывает readiness; следующая регистрация сохраняет
исправленную карточку.

## Отмена

Владелец может отменить только `draft`. Команда устанавливает `cancelled`,
`cancelled_at`, `cancelled_by`, нормализованную optional reason, завершает
dialog, увеличивает version и пишет audit events. Физического удаления нет.
Cancelled не является active draft; следующий intake без request ID создаёт
новый. Повтор с новым ключом возвращает 409 `already cancelled`.

## Idempotency и optimistic locking

Namespace — `(user_id, command_type, idempotency_key)`, где command type равен
`confirm`, `return_to_editing` или `cancel`. Fingerprint включает user, request,
command type и нормализованный payload, но не version, время или результат.
Replay проверяется до stale-version/status и возвращает прежний результат без
роста version, нового номера или logs. Тот же namespace с другим payload даёт
409. Одинаковый внешний ключ допустим для разных пользователей и типов команд.

Любая новая mutation требует `expected_version`. При stale confirmation номер
не выделяется. PostgreSQL sequence может получить пропуск только при ошибке
после `nextval`; сама заявка при этом остаётся draft.

## Атомарность и audit

Migration 008 создаёт одну внутреннюю transactional function и узкие RPC для
confirm/edit/cancel. Request update, dialog state, incoming/outgoing logs и
idempotency result фиксируются вместе. InMemory UoW моделирует rollback на
каждой критической стадии.

Lifecycle events отличаются от intake question logs:

- incoming: `confirm_command`, `return_to_editing_command`, `cancel_command`;
- outgoing: `request_registered`, `request_returned_to_editing`,
  `request_cancelled`.
- controlled failures: `lifecycle_conflict`, а недоступность persistence —
  best-effort `lifecycle_error`.

Логи содержат только безопасные IDs, command/version/result metadata,
request number и duration — без секретов, prompts и traceback.

## API

- `GET /api/v1/requests/{request_id}/confirmation?user_id=...`;
- `POST /api/v1/requests/{request_id}/confirm`;
- `POST /api/v1/requests/{request_id}/return-to-editing`;
- `POST /api/v1/requests/{request_id}/cancel`;
- `GET /api/v1/requests/by-number/{request_number}?user_id=...`.

Ошибки отображаются как 403/404/409/422/503 без traceback. Сейчас `user_id`
является техническим параметром, а не transport-authenticated identity.
Endpoints нельзя считать production-safe до привязки пользователя к
проверенному токену или каналу.

## Demo и migration

Offline demo: `python scripts/demo_request_lifecycle.py`.

Нужна подготовленная, но не применённая migration
`scripts/sql/008_request_lifecycle.sql`: существующая схема не содержит всех
timestamps/actors, lifecycle command store, sequence и RPC. Перед применением
используется read-only preflight и
`docs/request_lifecycle_migration_runbook.md`.
