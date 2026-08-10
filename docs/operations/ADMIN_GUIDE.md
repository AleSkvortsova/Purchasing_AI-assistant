# Руководство администратора и эксплуатации

**Проект:** «ИИ-ассистент по внутренним заявкам на закупку»
**Интерфейс:** Telegram-бот «Закупкин»
**Версия:** MVP 1.0
**Дата:** 4 августа 2026 года

## 1. Назначение

Документ описывает локальный запуск, диагностику и фактическую схему
эксплуатации MVP. Архитектурные детали приведены в
[`TECHNICAL_ARCHITECTURE.md`](../technical/TECHNICAL_ARCHITECTURE.md), границы —
в [`FINAL_TECHNICAL_SPECIFICATION.md`](../final/FINAL_TECHNICAL_SPECIFICATION.md),
а работы до пилота — в
[`PRODUCT_BACKLOG.md`](../planning/PRODUCT_BACKLOG.md).

## 2. Фактическая архитектура запуска

Локально FastAPI и Telegram-бот запускаются отдельными процессами. Production
использует VPS, Python virtual environment, systemd, Telegram long polling,
Supabase PostgreSQL/pgvector и OpenAI API. Docker, n8n и webhook в рабочий
контур не входят. Остальная часть документа является практическим runbook.

## 3. Область runbook

Следующие разделы охватывают установку, конфигурацию, Supabase, базу знаний,
проверки, production-обновление, наблюдение и восстановление после сбоев.

## 4. Требования

Поддерживается Python `>=3.11,<3.13`. Для локальной разработки нужен Git;
рекомендуется PowerShell в Windows. Production-путь подтверждён для Linux VPS
с systemd. Точные версии Python-зависимостей закреплены в `uv.lock`.

## 5. Клонирование репозитория

```powershell
git clone https://github.com/AleSkvortsova/Purchasing_AI-assistant.git
```

```powershell
Set-Location Purchasing_AI-assistant
```

## 6. Создание virtual environment

Windows:

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

## 7. Установка зависимостей

Для точного воспроизведения lock-файла:

```powershell
uv sync --extra dev --locked
```

Если `uv` не используется:

```powershell
python -m pip install -e ".[dev]"
```

## 8. Переменные окружения

Скопируйте `.env.example` в локальный `.env` и заполните только необходимые
значения. Не публикуйте содержимое `.env`.

| Группа | Переменные |
|---|---|
| Приложение | `APP_ENV`, `LOG_LEVEL`, `APP_TIMEZONE`, `API_V1_PREFIX` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_EXTRACTION_MODE`, `TELEGRAM_EXTRACTION_DEBUG` |
| Supabase | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| OpenAI | `OPENAI_API_KEY`, `APPROVAL_EXTRACTION_MODEL`, `RAG_ANSWER_MODEL` |
| Embeddings и поиск | `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_BATCH_SIZE`, `RAG_TOP_K`, `RAG_RETRIEVAL_MODE`, параметры кандидатов и RRF |
| Защита индексации | `ENABLE_RAG_INDEX_ENDPOINT` — по умолчанию `false` |

Telegram-бот требует токен и серверную конфигурацию Supabase. Без ключа OpenAI
оформление переходит в ограниченный режим `rule`, а ответы по регламенту
недоступны. `DATABASE_URL` зарезервирован и для штатного запуска не требуется.

## 9. Локальный запуск FastAPI

```powershell
uvicorn app.main:app --reload
```

Swagger доступен по адресу `http://127.0.0.1:8000/docs`.

## 10. Локальный запуск Telegram-бота

```powershell
python -m app.bot
```

Процесс использует long polling. Не запускайте одновременно два экземпляра с
одним токеном.

## 11. Проверка health endpoints

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/db/health
```

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/rag/health
```

Первые два endpoint возвращают `ok`. Проверка базы без конфигурации возвращает
контролируемый `not_configured`, а не traceback.

## 12. Структура Supabase

Основные таблицы: `users`, `requests`, `dialog_states`, `message_logs`,
`knowledge_documents`, `knowledge_chunks`, `approval_base_rules`,
`approval_additional_rules` и `request_lifecycle_commands`. Подробности и
таблица статусов находятся в
[`../technical/database_schema.md`](../technical/database_schema.md).

Ключ `service_role` используется только сервером. Клиенту Telegram он не
передаётся. До реального пилота требуется отдельное решение по RLS и модели
минимальных прав.

## 13. Применение SQL-миграций

Приложение не применяет миграции автоматически. Для чистого развёртывания
выполняйте файлы целиком в Supabase SQL Editor в порядке `001`–`009`, включая
исправляющие `004`, `006` и `009`. Перед `007` и `008` выполните соответствующие
read-only preflight-файлы. Уже применённые миграции не редактируйте и не
переигрывайте как новый источник истории.

Актуальный список находится в `scripts/sql/`. Для `007` и `008` используйте
[`../technical/intake_persistence_migration_runbook.md`](../technical/intake_persistence_migration_runbook.md)
и
[`../technical/request_lifecycle_migration_runbook.md`](../technical/request_lifecycle_migration_runbook.md).
Миграции нельзя выполнять без резервной копии, окна работ и проверки целевого
проекта.

## 14. Подготовка базы знаний

```powershell
python scripts/prepare_knowledge_base.py
```

Скрипт проверяет 14 рабочих документов, обновляет `manifest.json` и создаёт
файлы в `data/processed`. Ошибки блокируют публикацию; предупреждения нужно
просмотреть вручную.

## 15. Создание чанков

Чанки создаются той же командой подготовки. Проверьте
`data/processed/knowledge_chunks.json` и
`data/processed/chunk_statistics.json`. Не редактируйте подготовленные чанки
вручную: исправляйте исходный Markdown и повторяйте подготовку.

## 16. Валидация базы знаний

Проверьте `data/processed/validation_report.json`. Известные повторяющиеся
заголовки документа 14 допустимы, если иных ошибок нет. Затем выполните
безопасную проверку индексации без внешних клиентов:

```powershell
python scripts/index_knowledge_base.py --dry-run
```

## 17. Индексация в Supabase

Команда ниже выполняет платные вызовы OpenAI и записи в Supabase. Запускайте её
только после согласования и проверки окружения:

```powershell
python scripts/index_knowledge_base.py
```

Индексатор переиспользует неизменившиеся embeddings, заменяет изменившиеся
чанки по `(document_id, chunk_index)` и удаляет устаревшие записи только после
успешного сохранения актуального набора. Ключ `--force-reembed` требует
отдельного основания.

## 18. Проверка поиска

```powershell
python scripts/search_knowledge_base.py "Кто согласует закупку на 180000 рублей?" --mode hybrid --top-k 5 --debug-scores
```

Команда использует OpenAI embedding и read-only RPC Supabase. Для локальной
архитектурной проверки без сети используйте:

```powershell
python scripts/evaluate_retrieval.py --offline --mode all --top-k 5 --show-failures
```

## 19. Автоматические тесты

```powershell
pytest
```

Тесты используют управляемые имитации внешних зависимостей и не должны
обращаться к Telegram, OpenAI или production Supabase.

## 20. Ruff

```powershell
ruff check .
```

## 21. Проверка Git diff

```powershell
git diff --check
```

Дополнительно перед выпуском выполните `git status --short` и проверьте состав
изменений вручную.

## 22. Фактическая production-схема

Production MVP — один процесс Telegram-бота под systemd на VPS. Данные и
векторный индекс находятся в Supabase, извлечение и формирование справочных
ответов используют OpenAI. FastAPI можно запускать отдельно для технических
проверок; он не является промежуточным HTTP-слоем для Telegram-адаптера.

## 23. Каталог приложения

Подтверждённый каталог развёртывания:

```text
/opt/purchasing-ai-assistant
```

В каталоге находятся рабочая копия Git и `.venv`. Секреты не должны быть
доступны другим пользователям сервера.

## 24. Окружение `.venv`

Проверка интерпретатора:

```bash
/opt/purchasing-ai-assistant/.venv/bin/python --version
```

Установка текущего пакета:

```bash
/opt/purchasing-ai-assistant/.venv/bin/python -m pip install -e /opt/purchasing-ai-assistant
```

## 25. systemd unit

Имя рабочего сервиса:

```text
purchasing-ai-bot.service
```

Файл unit не хранится в репозитории, поэтому перед изменением проверьте его
фактические `WorkingDirectory`, `ExecStart`, пользователя процесса и способ
подключения окружения:

```bash
sudo systemctl cat purchasing-ai-bot.service
```

## 26. Безопасный порядок обновления

1. Зафиксируйте текущий commit и состояние сервиса.
2. Проверьте резервную копию данных перед миграциями.
3. Остановите сервис.
4. Получите только fast-forward изменения.
5. Обновите зависимости в `.venv`.
6. Примените отдельно согласованные новые миграции и обновление базы знаний.
7. Запустите проверки, затем сервис.
8. Выполните контрольный Telegram-сценарий и просмотр журналов.

```bash
cd /opt/purchasing-ai-assistant
```

```bash
sudo systemctl stop purchasing-ai-bot.service
```

```bash
git pull --ff-only
```

```bash
.venv/bin/python -m pip install -e .
```

```bash
sudo systemctl start purchasing-ai-bot.service
```

## 27. Проверка статуса сервиса

```bash
sudo systemctl status purchasing-ai-bot.service --no-pager
```

Ожидается состояние `active (running)`. Проверьте, что процесс не уходит в
цикл рестартов.

## 28. Просмотр журналов

```bash
sudo journalctl -u purchasing-ai-bot.service -n 200 --no-pager
```

Для наблюдения за новыми сообщениями:

```bash
sudo journalctl -u purchasing-ai-bot.service -f
```

В журналах не должны появляться токены, ключи, полный пользовательский текст,
системные инструкции, полный ответ модели или содержимое найденных фрагментов.

## 29. Контрольный тест после развёртывания

1. Отправьте `/start` и проверьте пять пунктов главного меню.
2. Откройте «Инструкция» и вернитесь в меню.
3. Создайте синтетический черновик и убедитесь, что он восстанавливается.
4. Не регистрируйте тестовую заявку без согласованной необходимости.
5. Задайте заранее утверждённый вопрос по регламенту и проверьте источник.
6. Просмотрите service logs на контролируемые ошибки без чувствительных данных.

## 30. Восстановление после неудачного обновления

Не откатывайте применённые миграции удалением таблиц или редактированием их
истории. Остановите сервис, зафиксируйте ошибку, переключите приложение на
заранее известный совместимый commit и восстановите зависимости. Если новая
схема несовместима со старым кодом, требуется отдельная forward-fix версия, а
не произвольный rollback SQL.

```bash
git rev-parse HEAD
```

```bash
git switch --detach <KNOWN_GOOD_COMMIT>
```

После восстановления повторите шаги 27–29. Возврат на основную ветку и
исправление оформляются отдельным согласованным изменением.

## 31. Недоступность OpenAI

Проверьте наличие переменных без вывода их значений, сетевую доступность и
безопасные поля ошибки: тип, HTTP status, code, param и request id. Ошибка
извлечения в `hybrid` должна привести к программному резервному режиму.
Справочные ответы без OpenAI недоступны. Не выводите prompt, request body или
raw response.

## 32. Недоступность Telegram

Проверьте статус systemd, формат токена без его печати, отсутствие второго
polling-процесса и сетевой доступ к Telegram. После устранения причины
перезапустите сервис и выполните `/start`.

## 33. Недоступность Supabase

Проверьте `SUPABASE_URL` и наличие серверного ключа без вывода значений,
`/api/v1/db/health`, статус проекта и журналы сетевых ошибок. Различайте
неверную конфигурацию, отсутствие таблиц, `permission denied` и сетевой сбой.
Не заменяйте проблему доступа расширением прав `anon`/`authenticated` и не
передавайте `service_role` клиенту.

## 34. Работа с секретами

- `.env` исключён из Git и должен иметь минимальные права доступа;
- не вставляйте секреты в Markdown, issue, commit message или журнал;
- при подозрении на утечку отзовите и замените Telegram token, OpenAI key и
  Supabase key;
- не передавайте полный request/response внешних API в диагностику;
- регулярно проверяйте Git и журналы на случайную публикацию.

## 35. Ограничения безопасности MVP

Не подтверждены RLS для реального клиентского доступа, SSO, формальная политика
хранения и удаления данных, нагрузочная устойчивость, автоматический мониторинг
и восстановление из резервной копии. Контекст уточнения по регламенту
обновляется неатомарно. Серверный `service_role` имеет широкие права.

## 36. До реального пилота

Обязательны пункты P0 из [`PRODUCT_BACKLOG.md`](../planning/PRODUCT_BACKLOG.md): модель
минимальных прав и RLS, управление секретами, нагрузочные и параллельные тесты,
мониторинг, политика хранения, проверенное резервное восстановление и
атомарное обновление контекста уточнений. Экономическая оценка приведена в
[`COST_ESTIMATE_FINAL.md`](../final/COST_ESTIMATE_FINAL.md), а роль LLM и программных
правил — в
[`SYSTEM_PROMPTS_AND_RULES_FINAL.md`](../final/SYSTEM_PROMPTS_AND_RULES_FINAL.md).

Отдельная инструкция закупщика не создаётся: кабинет, очередь, комментарии,
возврат зарегистрированной заявки и изменение статусов закупщиком в MVP не
реализованы.
