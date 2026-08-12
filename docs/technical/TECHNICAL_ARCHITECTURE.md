# Техническая архитектура MVP

**Проект:** «ИИ-ассистент по внутренним заявкам на закупку»
**Версия:** MVP 1.0
**Дата фиксации:** 4 августа 2026 года

Документ описывает только инженерное устройство реализованного решения.
Продуктовые границы и план/факт приведены в
[`../final/FINAL_TECHNICAL_SPECIFICATION.md`](../final/FINAL_TECHNICAL_SPECIFICATION.md),
результаты проверок — в
[`../final/ACCEPTANCE_TEST_REPORT.md`](../final/ACCEPTANCE_TEST_REPORT.md),
разделение ответственности модели и кода — в
[`../final/SYSTEM_PROMPTS_AND_RULES_FINAL.md`](../final/SYSTEM_PROMPTS_AND_RULES_FINAL.md),
дальнейшая разработка — в
[`../planning/PRODUCT_BACKLOG.md`](../planning/PRODUCT_BACKLOG.md).

## 1. Архитектурные потоки

Оформление заявки:

```text
Telegram / aiogram long polling
  → Telegram adapter
  → OpenAI Structured Outputs + программный разбор
  → объединение, нормализация и проверка полноты
  → категория, следующий вопрос или карточка
  → persistence orchestration
  → Supabase RPC / PostgreSQL
  → confirm, edit или cancel
```

Вопрос по регламенту:

```text
Telegram, изолированный режим Regulation Q&A
  → understanding, intents и slots
  → при необходимости persistent clarification
  → embeddings + Russian full-text search
  → RRF по позициям результатов
  → пять релевантных фрагментов
  → OpenAI answer provider
  → проверка утверждений, значений и источников
  → ответ либо контролируемый отказ
```

FastAPI предоставляет health- и технические endpoints. Telegram-адаптер
вызывает внутренние сервисы напрямую и не ходит в собственный HTTP API.
Бизнес-логика не дублируется в Telegram handlers.

## 2. Структура модулей

- `app/api/` — HTTP-маршруты и зависимости FastAPI;
- `app/bot/` — aiogram wiring, handlers, adapter и форматирование;
- `app/extraction/` — Structured Outputs, программный разбор, нормализация и
  проверка evidence;
- `app/intake/` — реестр полей, состояние диалога, merge, completeness,
  категории и карточка;
- `app/persistence/` и repositories — оркестрация хранения и Supabase RPC;
- `app/rules/` — детерминированный предварительный маршрут согласования;
- `app/rag/` — understanding, retrieval, формирование и проверка справочного
  ответа;
- `scripts/` — подготовка, индексация, evaluation и эксплуатационная
  диагностика;
- `scripts/sql/` — последовательная история миграций 001–009.

## 3. Модель данных

Основные таблицы:

| Таблица | Назначение |
|---|---|
| `users` | внутренний UUID, уникальный `telegram_id`, подразделение, роль и активность |
| `requests` | владелец, тип, категория, заголовок, статус, номер, JSONB `data`, версия и lifecycle timestamps |
| `dialog_states` | режим (`idle`, `intake`, `regulation_qa`), активная заявка и `state_data` |
| `message_logs` | безопасные технические и audit-события |
| `knowledge_documents` | метаданные версий источников |
| `knowledge_chunks` | текстовые фрагменты, FTS-данные, metadata и embeddings |
| `approval_base_rules`, `approval_additional_rules` | структурированные правила маршрута |
| `request_lifecycle_commands` | идемпотентность lifecycle-команд и сохранённые результаты |

Подробная схема и права функций описаны в
[`database_schema.md`](database_schema.md).

## 4. Канонический черновик и проекции

Канонический источник текущих данных — `requests.data.intake.draft`,
соответствующий `RequestDraftData`. Он хранит значения, состояния полей,
источник, evidence из сообщения пользователя, конфликты и предупреждения.

Колонки `request_type`, `category_code`, `title` и legacy-поля верхнего уровня
JSON являются синхронизированными проекциями. Миграция 009 и актуальная
оркестрация синхронизируют их с каноническим черновиком. При регистрации
`data.intake.intake_status` становится `completed`, а `next_question`
очищается.

Модель содержит один основной предмет и один набор
`quantity`/`unit`/`specifications`; массива построчных позиций нет. Тип закупки
в канонической модели — только `goods` или `service`. Legacy-вход `work`, если
встречается на границе, нормализуется в `service` и не сохраняется как
актуальное значение.

## 5. Intake и сохранение состояния

Режимы извлечения:

- `rule` — локальный консервативный разбор;
- `openai` — Structured Outputs без скрытого fallback;
- `hybrid` — OpenAI с программной проверкой и fallback при ошибке provider;
- `fake` — детерминированный provider тестов.

Точно распознанные программой количество, единица, сумма и срок не заменяются
`null` или менее точным значением модели. Код проверяет enum, числа, даты,
единицы, бюджетный статус, обязательность, конфликты и готовность карточки.
Положительные извлечённые факты требуют evidence; отсутствие упоминания может
обосновывать `false` без искусственного фрагмента.

Явный deterministic `procurement_type` с evidence имеет тот же приоритет при
structured null, rejection и provider fallback. Совпадающий structured type
подтверждает результат; противоположный тип очищается вместе с несовместимой
категорией и переводит flow в контролируемое уточнение вместо silent overwrite.

`IntakeConversationState` хранит ожидаемое поле, вопрос, этап, кандидатов
предметов и категорий, показанные варианты, выбранную категорию, fingerprint и
счётчик повторов. Category option дополнительно хранит provenance и отдельные
признаки допустимости показа и readiness. Goods и services используют общий
механизм. Strong candidates переживают перезагрузку adapter через
`dialog_states.state_data`; выбрать можно номер, полное или частичное название.
Generic fallback и legacy option без provenance считаются weak и не доказывают
совместимость категории.

Для предметов вне deterministic vocabulary category resolution использует
безопасный второй уровень: отдельный strict Structured Output вызывается через
тот же экземпляр OpenAI client, что и основной extraction provider. Модель видит
только `goods` или `service`, предмет, релевантное описание и закрытую taxonomy
соответствующего типа. Taxonomy строится из канонического `CATEGORY_NAMES` и
versioned semantic metadata `intake-categories-v3`; RAG и knowledge index в этом
решении не участвуют.

`v3` сохраняет уточнённую границу сервисных категорий: S01 охватывает ремонт, монтаж,
регламентное обслуживание и восстановление работоспособности оборудования;
S15 — специализированные технические услуги, которые не являются такими
работами, включая метрологию, поверку, калибровку, испытания и экспертизу.
Для товаров G03 охватывает вычислительную и сетевую инфраструктуру, G04 —
периферию рабочего места, G14 — электротехнические материалы и компоненты, а
G15 — самостоятельное инженерное/промышленное оборудование и его запчасти.
Это семантическое описание taxonomy, а не deterministic словарь предметов.

Evidence категории сначала проверяется как нормализованный фрагмент, а затем —
как непрерывная последовательность тех же содержательных токенов с безопасным
лёгким stemming русских окончаний. Одного общего слова недостаточно: изменение
предметного смысла по-прежнему приводит к `invalid_evidence`.

Решение `llm_exact` или `llm_candidates` сохраняется только в
`dialog_states.state_data` и не заполняет draft. После явного выбора поле получает
provenance `llm_confirmed`, связанный с fingerprint `procurement_type + item_name`
и выбранным кодом. Completeness и lifecycle повторно проверяют этот fingerprint.
Unconfirmed LLM suggestion, malformed/provider failure и generic fallback не дают
readiness. Изменение предмета или типа делает сохранённые candidates недействующими.

Conversational mode не владеет intake draft. Явный вход в Regulation Q&A
очищает только старый regulation pending; `/start` переводит mode в `idle` и
также очищает pending. В обоих случаях `active_request_id` и canonical draft
сохраняются. Переход в intake очищает regulation pending.

`save_intake_step` транзакционно обновляет версию заявки, канонический draft,
состояние диалога, входящее сообщение и результат шага. Optimistic locking не
позволяет перезаписать более новую версию. Черновик сохраняется при навигации и
перезапуске процесса.

## 6. Lifecycle, idempotency и нумерация

Статусы записи:

- `draft` — редактируемый черновик;
- `new` — зарегистрирована, пользователю показывается «Передана в отдел
  закупок»;
- `cancelled` — отменена без физического удаления.

Confirm допустим только владельцу актуального `draft` в состоянии
`ready_for_confirmation`. Edit возвращает карточку к редактированию, cancel
переводит её в `cancelled`. Lifecycle-команда имеет idempotency key; повтор
возвращает сохранённый результат, а stale version — контролируемую ошибку.

Регистрация блокирует строку и повторно проверяет владельца, статус и версию.
Номер `PR-YYYY-NNNNNN` выдаёт PostgreSQL sequence, поэтому гонка `max()+1`
исключена, а пропуски после rollback допустимы. Draft и cancelled номера не
получают.

При confirm сохраняется неизменяемый `lifecycle.final_request_card` с
предварительным маршрутом, результатом completeness, версиями реестра и правил,
временем и пользователем подтверждения. Поздняя синхронизация проекций не
переписывает этот исторический снимок.

## 7. База знаний, чанки и индексация

База содержит 14 рабочих Markdown-документов, служебный `00_README.md` и
`manifest.json`. Подготовка проверяет front matter, версии, даты, приоритеты,
checksum и структуру, затем детерминированно формирует чанки.

`chunk_id` зависит от содержимого. Индексатор выполняет upsert по
`(document_id, chunk_index)`, может обновить `id`, сохраняет embedding нового
содержимого и удаляет stale chunks только после успешного сохранения всех
актуальных. Неизменившиеся embeddings переиспользуются.

Состав и выпуск базы описаны в
[`../final/KNOWLEDGE_BASE_GUIDE.md`](../final/KNOWLEDGE_BASE_GUIDE.md), детали
подготовки и индексации — в
[`knowledge_base_preparation.md`](knowledge_base_preparation.md) и
[`rag_indexing.md`](rag_indexing.md).

## 8. Гибридный поиск и проверка источников

Перед построением поисковых вариантов deterministic understanding принимает
явное решение `known_domain_intent`, `ambiguous_domain` или `outside_domain`.
Неизвестный текст не получает закрытый fallback `required_fields`. Retrieval
разрешён только при положительном закупочном сигнале; outside-domain вопрос
останавливается до query expansion и не получает источников. Активный pending
clarification является отдельным подтверждённым контекстом, поэтому короткие
ответы «да», «нет» и «не знаю» продолжают многошаговый диалог.

Domain decision учитывает explicit purpose: личное назначение без рабочего
контекста отклоняется, организационное принимается, а их сочетание требует
уточнения. Cancellation распознаётся по action/target конструкции независимо
от расстояния между глаголом и объектом.

Semantic retrieval использует cosine similarity embeddings. Lexical retrieval
применяет Russian FTS (с fallback на `simple`) к заголовку, пути раздела и
содержимому. Для числовых вопросов используются strict, text-only и broad
представления; числа не становятся обязательными токенами, а broad terms
соединяются через OR.

Hybrid RPC объединяет позиции semantic и lexical списков методом Reciprocal
Rank Fusion. По умолчанию рассматриваются 20 + 20 кандидатов, `RRF_K=60`, веса
равны, answer provider получает до пяти прошедших intent/relevance filtering
фрагментов. Raw lexical scores нужны только для порядка lexical candidates и
не смешиваются с позициями RRF.

Answer provider получает только выбранные фрагменты. Каждое утверждение
связывается с источником; приложение проверяет идентификатор, нормативную
допустимость, подтверждение claim, конкретные значения и ограничение
дословного копирования. Relevance validation требует соответствия primary
intent исходного вопроса: secondary intent может дополнить, но не заменить
основной ответ. Examples/templates не доказывают нормативные правила.
Вопросы об актуальных ценах и поставщиках получают controlled refusal.

Для approval route сумма и бюджетный статус извлекаются в slots, а диапазон и
маршрут вычисляет приложение. Модель не выводит маршрут заново.

## 9. Многошаговое уточнение Regulation Q&A

Pending clarification хранится только в `dialog_states.state_data` и содержит
исходный вопрос, intents, известные и недостающие slots, последний вопрос,
fingerprint, номер шага и время создания. Raw chunks, prompt и model payload не
сохраняются.

Короткий ответ сначала объединяется с pending slots; исходный intent имеет
приоритет, если сообщение не является новым полным вопросом. Контекст очищается
после ответа, выхода в меню, смены режима, нового полного вопроса или TTL 30
минут. Уточнение ограничено тремя шагами; `budget_status=unknown` приводит к
безопасному объяснению, а не к циклу.

Известное ограничение: pending state обновляется по схеме read–modify–write без
версии строки, поэтому параллельные сообщения теоретически могут потерять одно
обновление. Intake persistence и lifecycle используют отдельные атомарные RPC.

## 10. Детерминированный маршрут согласования

Базовые правила используют сумму и `budgeted`/`unbudgeted`; дополнительные —
срочность, единственного поставщика, S11, доступ к данным и работу на объекте.
Модуль:

- не вызывает LLM или RAG;
- не выполняет фактическое согласование;
- не меняет заявку;
- возвращает предварительные роли и идентификаторы правил;
- при неизвестном бюджетном статусе требует уточнение.

Реализация и SQL-контракты описаны в
[`approval_rule_engine.md`](approval_rule_engine.md).

## 11. Безопасность и логирование

- `service_role` используется только сервером;
- ownership проверяется при чтении и изменении заявки;
- EXECUTE критичных RPC отозван у `PUBLIC`, `anon` и `authenticated` и выдан
  `service_role`;
- optimistic locking, транзакции и idempotency защищают состояние;
- API keys, Authorization headers, prompts, полный вопрос, retrieved chunks,
  request body и raw model response не журналируются;
- безопасный RAG log содержит fingerprint, статус, количество фрагментов и
  источников, длительность и reason code;
- индексирующий API по умолчанию отключён.

До пилота нужны RLS, ротация секретов, формальная политика хранения,
мониторинг, проверенное восстановление и отдельный security review.

## 12. Production-развёртывание

Подтверждённая схема использует VPS, каталог
`/opt/purchasing-ai-assistant`, `.venv`, установку Python-пакета и systemd unit
`purchasing-ai-bot.service`. Telegram работает через long polling; Docker и
webhook не используются. Supabase остаётся внешней PostgreSQL/pgvector
платформой.

Обновление выполняется остановкой сервиса, `git pull --ff-only`, установкой
актуального пакета и запуском с проверкой `systemctl status` и `journalctl`.
Практический runbook находится в
[`../operations/ADMIN_GUIDE.md`](../operations/ADMIN_GUIDE.md).

## 13. Технические ограничения

- один активный черновик и один основной предмет без массива позиций;
- перед scalar draft действует промежуточный `ProcurementDecomposition`:
  очевидная пара goods+service сохраняется в `dialog_states.state_data`, после
  чего пользователь выбирает одну отдельную заявку;
- decomposition использует высокоточные action-based признаки и программную
  validation; неявное mixed-намерение по-прежнему может потребовать уточнения;
- category candidates связаны с типом и fingerprint decomposition, а semantic
  compatibility с positive support повторно проверяется при readiness и
  lifecycle confirm; unknown subject не может стать confirmable только по
  G/S-префиксу;
- полноценного массива `request_items` и автоматической регистрации обеих
  потребностей нет;
- нет отдельного поля даты начала услуги;
- история ограничена пятью read-only записями;
- typo tolerance и ранжирование общих категорий ограничены;
- нет reranker; числовые решения вынесены в deterministic processing;
- без OpenAI intake использует rule fallback, а Regulation Q&A недоступен;
- pending Regulation Q&A обновляется неатомарно;
- нет длительных многопользовательских, нагрузочных и security-тестов;
- нет RLS-ready пользовательского API, кабинета закупщика и фактического
  согласования;
- ответы ограничены индексированной учебной базой и не используют внешние
  актуальные данные.
Structured intake extraction определяет `goods`/`service` семантически, даже
когда пользователь не использует буквальный глагол «купить». Deterministic
правила остаются быстрым shortcut; конструкция назначения вида «для установки»
сама по себе не превращает материальный предмет в услугу.

Category provider получает накопленный `CategoryResolutionContext`, собранный
из prospective canonical draft, а не последнюю Telegram-реплику. Subject
fingerprint продолжает защищать подтверждённую категорию от смены предмета;
context fingerprint отдельно управляет retry после содержательного уточнения.

Версия `intake-categories-v3` сохраняет закрытый набор G01–G15/S01–S15, но
устраняет два пробела исходной учебной taxonomy. G03 включает основные
вычислительные и инфраструктурные IT-устройства, включая сетевую
инфраструктуру; G04 остаётся периферией и аксессуарами компьютерного рабочего
места. G15 включает самостоятельное инженерное/промышленное оборудование и его
детали, компоненты и запчасти. Граница определяется по назначению, а не через
список названий предметов.

Canonical business source — `knowledge_base/04_Классификатор_категорий_закупок.md`;
runtime names и semantic metadata синхронизированы в field registry. Коды
категорий не участвуют в базовом маршруте по сумме и бюджету; единственное
дополнительное category-specific approval rule относится к S11. Поэтому
уточнение смысла G03/G15 не изменяет approval routing и не требует SQL migration.
