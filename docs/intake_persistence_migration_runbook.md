# Runbook применения migration 007

Migration 007 пока только подготовлена. Все действия ниже выполняются в
согласованное окно владельцем Supabase-проекта. Не вставляйте service role key,
токены или пользовательские данные в команды, скриншоты и отчёт.

## 1. Backup/checkpoint

- Зафиксируйте проект, окружение, время окна и ответственного.
- Создайте штатный backup/checkpoint Supabase и убедитесь, что он завершён.
- Сохраните текущие counts/status из первого result set preflight.
- Остановите rollout новых intake endpoints на время проверки схемы.

## 2. Read-only preflight

Откройте SQL Editor, загрузите содержимое
`scripts/sql/007_intake_persistence_preflight.sql` и выполните его отдельно.
Файл содержит только SELECT. Сохраните все девять result sets и время запуска.

## 3. Duplicate active drafts

Result set 2 должен быть пустым. Если он содержит строки, migration намеренно
остановится до создания unique index. Зафиксируйте user ID, все request IDs и
`updated_at`; не выбирайте победителя только по самой новой дате.

## 4. Ручное разрешение дублей

Владелец данных определяет канонический draft по содержимому и истории. Для
остальных заявок выбирается допустимый бизнес-статус либо иной согласованный
способ сохранения. Этот runbook не содержит автоматического UPDATE/DELETE:
исправление должно быть отдельным проверяемым change с перечнем затронутых ID.
Аналогично вручную разрешаются duplicate idempotency keys из result set 3.
После изменения данных снова запустите preflight и получите пустые sets 2–6.

## 5. Применение migration 007

В SQL Editor выполните целиком
`scripts/sql/007_intake_persistence_orchestration.sql`. Файл обёрнут в
`BEGIN/COMMIT`; при найденных дублях или другой ошибке DDL откатывается. Не
выполняйте отдельные фрагменты и не продолжайте после ошибки без повторного
preflight.

## 6. Повторный запуск

После успешного применения выполните тот же файл 007 второй раз целиком.
Повтор должен завершиться без duplicate object/privilege errors. Это проверяет
идемпотентность DDL и повторное сужение EXECUTE grants.

## 7. Проверка схемы и прав

Снова выполните read-only preflight. Проверьте:

- `requests.version` — `bigint`, `not null`, default 1;
- полный набор новых колонок `message_logs`;
- оба именованных partial unique index;
- одну сигнатуру `public.save_intake_step(...)`;
- owner `postgres`, `security_definer = true`;
- EXECUTE отсутствует у `PUBLIC`, `anon`, `authenticated` и есть только у
  `service_role` среди клиентских ролей;
- sets 2–6 пусты.

Default `version = 1` — безопасный baseline для legacy rows, а не попытка
восстановить историческое число обновлений.

## 8. Smoke test RPC

Из доверенного server-side окружения выполните один тестовый шаг на специально
созданном тестовом пользователе/черновике. Проверьте: version увеличилась ровно
на 1, dialog state имеет ту же версию, созданы ровно incoming и outgoing logs.
Повторите тот же idempotency key и payload: должна вернуться та же версия с
`replayed=true`, без новых логов. Повтор с изменённым payload должен дать
idempotency conflict. Не используйте production-заявку.

## 9. Smoke test API

В закрытом тестовом окружении последовательно проверьте:

1. Первый structured step создаёт один draft и сохраняет шаг.
2. Следующий step без request ID возобновляет тот же draft.
3. Повтор того же key/payload возвращает replay без роста версии.
4. Explicit correction создаёт новую версию и сохраняет историю.
5. Active-session возвращает восстановленное состояние и карточку.

До transport authentication не открывайте endpoint внешним клиентам: body
`user_id` является техническим MVP-параметром, а не проверенной identity.

## 10. Rollback notes

Автоматического rollback нет. При необходимости сначала остановите потребителей
RPC и соберите перечень созданных после rollout данных. Удаление функции,
индексов или колонок требует отдельной согласованной migration после проверки
всех потребителей. Не удаляйте message logs или requests как часть аварийного
отката.

## 11. Partial application

При любой ошибке сохраните полный безопасный текст SQL error и не запускайте
отдельные оставшиеся statements. Проверьте состояние транзакции новым запуском
preflight. Если columns/indexes/function показывают смешанное состояние,
остановите rollout, сравните схему с 007 и согласуйте repair migration. Не
исправляйте production schema вручную без перечня изменений и второго ревью.

Отдельный пустой request может остаться, если его первый RPC-шаг упал уже после
создания draft. Это не частичное выполнение RPC. Такой draft используется
повтором; `system_error` log в недоступной БД является best effort. Автоочистка
не выполняется.

## 12. Что сохранить в отчёт

- проект/окружение, время и ответственный;
- backup/checkpoint ID;
- все preflight result sets до и после;
- вручную разрешённые user/request/message-log IDs и основание решения;
- результат первого и повторного применения 007;
- сигнатуру, owner, security flag и ACL функции;
- IDs тестового пользователя, request, dialog и двух logs;
- версии до/после, результат replay и conflict checks;
- API status codes первого шага, resume, replay, correction и active-session;
- оставшиеся риски и решение о rollout/rollback.
