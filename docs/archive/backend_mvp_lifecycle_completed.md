# Backend MVP: intake и lifecycle заявок завершены

## Дата фиксации

27 июля 2026 года.

## Что реализовано

- детерминированное intake-ядро и реестр полей заявки;
- persistence orchestration с одним активным черновиком;
- последовательный сбор, проверка полноты и формирование карточки;
- расчёт маршрута согласования;
- возврат к редактированию и повторная проверка готовности;
- подтверждение, регистрация с номером `PR-YYYY-NNNNNN` и отмена черновика;
- optimistic locking и идемпотентность lifecycle-команд;
- синхронизация canonical draft с колонками и legacy-проекциями.

## Что проверено автоматически

Итоговый полный прогон: **405 passed**. Статический анализ Ruff и проверка
форматирования Git diff также прошли успешно.

## Что проверено вручную

Через Swagger и реальную Supabase пройден основной сценарий от последовательного
заполнения draft до confirmation view, возврата к редактированию, повторной
готовности и регистрации. Проверены номер заявки, поиск по номеру, исключение
зарегистрированной заявки из active draft и создание следующего черновика.

Отдельно проверены отмена draft, исключение отменённой заявки из active session,
идемпотентные повторы confirm и cancel, а также первое и повторное применение
migration 009. Replay не изменил номер, version или terminal timestamps.

## Применённые миграции

- `scripts/sql/007_intake_persistence_orchestration.sql`;
- `scripts/sql/008_request_lifecycle.sql`;
- `scripts/sql/009_sync_request_data_projections.sql`.

Migration 009 исправила существующую зарегистрированную заявку, была успешно
применена повторно и не изменила `updated_at` уже синхронизированной строки.
Повторный запуск также сохранил `requests.version`, `final_request_card` и
`final_approval_route`.

## Обнаруженные и устранённые проблемы

1. После lifecycle confirm обнаружилась рассинхронизация `data.quantity` и
   `data.intake.draft.quantity`.
2. Причиной были дублирующиеся legacy-проекции значений заявки.
3. Единым canonical source закреплена модель `RequestDraftData`, сохранённая в
   `data.intake.draft`.
4. Проекции теперь полностью пересобираются из canonical draft при успешном
   intake update и регистрации.
5. Legacy PATCH не может менять поля, которыми управляет intake.
6. Immutable lifecycle snapshots `final_request_card` и
   `final_approval_route` не переписываются repair-операцией.
7. Repeat-safe migration 009 исправляет зарегистрированные заявки со статусом
   `new`, устанавливает `completed` и `next_question = null`, защищает intake и
   lifecycle от общего JSON merge и пересоздаёт registration trigger.

## Ограничения milestone

- Telegram UI ещё не реализован;
- полноценный пользовательский E2E через Telegram не проверен;
- административный интерфейс закупщика отсутствует;
- маршрут согласования рассчитывается, но не исполняется внешними согласующими;
- production deployment этого milestone отдельно не подтверждён.

## Следующий этап

Telegram adapter MVP.
