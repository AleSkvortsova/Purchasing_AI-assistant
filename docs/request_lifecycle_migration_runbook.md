# Runbook migration 008: request lifecycle

Migration не применяется автоматически. Не помещайте service role key,
токены, персональные данные или полный payload заявки в команды и отчёты.

## 1. Backup/checkpoint

Создайте штатный backup Supabase, сохраните checkpoint ID, проект, окружение,
время окна и ответственного. Приостановите rollout lifecycle endpoints.

## 2. Read-only preflight

Выполните целиком `scripts/sql/008_request_lifecycle_preflight.sql` в SQL
Editor и сохраните девять result sets. Файл содержит только SELECT.

## 3. Ручное разрешение проблем

- Set 1 — справочная статистика; сам по себе rollout не блокирует.
- Set 2 (duplicate request numbers) должен быть пуст; любая строка блокирует migration.
- Set 3 (`new` без номера либо `draft/cancelled` с номером) должен быть пуст;
  любая строка блокирует rollout до ручной сверки legacy-заявки.
- Set 4 (timestamps/actors, включая actor UUID без строки в `users`) должен быть
  пуст; любая строка блокирует migration, потому что FK или lifecycle-инвариант
  иначе не может быть безопасно установлен.
- Set 5 (active dialog для `new/cancelled`) должен быть пуст до rollout.
- Set 6 показывает расхождение persistence status и lifecycle JSON и должен быть
  разобран вручную до включения endpoints.
- Set 7 до migration ожидаемо не содержит новых columns; после migration должен
  содержать полный перечень из запроса.
- Set 8 до migration ожидаемо может быть пуст; после migration должен содержать
  три индекса и sequence `request_number_seq`.
- Set 9 до migration ожидаемо может быть пуст; после migration должен содержать
  шесть RPC с owner `postgres`, `security_definer=true` и ACL без PUBLIC/anon/
  authenticated execute.

Не исправляйте строки массовым автоматическим DELETE. Любые data fixes —
отдельное согласованное изменение с перечнем request/dialog IDs.

## 4. Применение migration 008

Выполните `scripts/sql/008_request_lifecycle.sql` целиком. Файл обёрнут в
BEGIN/COMMIT и до unique index проверяет старые номера. При ошибке не запускайте
оставшиеся statements отдельно: снова выполните preflight.

## 5. Повторное применение

После успешного применения выполните 008 второй раз целиком. Повтор не должен
расширять grants или выдавать duplicate-object errors.

## 6. Проверка схемы

Повторите preflight и проверьте:

- registration/cancellation columns и `requests.version`;
- partial unique index request number;
- `request_lifecycle_commands` и unique namespace;
- sequence `request_number_seq`;
- шесть ожидаемых function signatures, owner `postgres`, fixed search path и
  SECURITY DEFINER;
- внутренний apply RPC не доступен service role напрямую;
- confirm/edit/cancel/mark RPC доступны только service role среди клиентских
  ролей.

## 7. Smoke test confirmation

На синтетическом ready draft получите confirmation view, запомните version,
выполните confirm и проверьте `draft → new`, version +1, номер, timestamps,
actor, immutable snapshot, completed dialog и ровно два lifecycle logs.

## 8. Smoke test replay

Повторите confirm с тем же key/payload и старой expected version. Должны
вернуться тот же номер/version и `replayed=true` без новых logs. Новый key для
registered request должен дать 409. Тот же key с иным payload — conflict.

## 9. Smoke test stale version

На отдельном ready draft запомните version, выполните ещё один intake update и
попробуйте confirm со старой version. Ожидается 409; request остаётся `draft`,
номер не назначается, sequence не расходуется этим вызовом, dialog не становится
`completed`, success logs и idempotency result не создаются.

## 10. Smoke test return to editing

На отдельном ready draft выполните return-to-editing, измените amount обычным
intake step, снова получите ready view и зарегистрируйте. Финальный snapshot
должен содержать исправленную сумму.

## 11. Smoke test cancellation

Отмените отдельный draft с reason. Проверьте status/timestamps/actor, отсутствие
номера, cancelled dialog, replay и создание нового active draft следующим
intake step.

После регистрации и после отмены отдельно вызовите active-session lookup.
Terminal request не должен возвращаться как активный. Следующий intake без
request ID должен создать новый draft. Состояние `draft + completed/cancelled
dialog` считается повреждённым и требует ручного восстановления.

## 12. Проверка связей

Сохраните request ID/number/version, dialog state и lifecycle command/log IDs.
Убедитесь, что user/request IDs совпадают, terminal request не остаётся active,
а command result соответствует двум logs и snapshot.

Выполните read-only сверку для использованных request IDs:

```sql
select id, user_id, status, request_number, version, registered_at,
       confirmed_at, confirmed_by, cancelled_at, cancelled_by,
       data->'lifecycle' as lifecycle
from public.requests
where id in ('<request-uuid-1>'::uuid, '<request-uuid-2>'::uuid);

select user_id, active_request_id, current_step, state_data
from public.dialog_states
where user_id in ('<user-uuid-1>'::uuid, '<user-uuid-2>'::uuid);

select request_id, command_type, idempotency_key, fingerprint, result, created_at
from public.request_lifecycle_commands
where request_id in ('<request-uuid-1>'::uuid, '<request-uuid-2>'::uuid)
order by created_at;

select request_id, direction, message_type, lifecycle_command_type,
       lifecycle_idempotency_key, intake_status, payload, metadata
from public.message_logs
where request_id in ('<request-uuid-1>'::uuid, '<request-uuid-2>'::uuid)
  and lifecycle_command_type is not null
order by created_at;
```

## 13. Rollback notes

Автоматического rollback нет. Сначала остановите lifecycle endpoints. Удаление
RPC/index/table/columns/sequence требует отдельной reviewed migration после
проверки потребителей и зарегистрированных заявок. Не удаляйте requests,
snapshots, command records или audit logs. Sequence не гарантирует отсутствие
пропусков и не должна вручную уменьшаться после неуспешного вызова.
