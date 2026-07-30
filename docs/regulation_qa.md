# Grounded Q&A по регламентам

## Архитектура

Telegram Q&A не создаёт второй RAG. Цепочка использует существующие компоненты:

```text
Telegram regulation_qa
→ RegulationQuestionAnsweringService
→ deterministic query plan (до трёх вариантов)
→ KnowledgeRetrievalService
→ OpenAIEmbeddingProvider
→ SupabaseKnowledgeRepository
→ match_knowledge_chunks_hybrid RPC
→ OpenAIGroundedAnswerProvider
→ ответ и уникальные источники
```

Настройки `RAG_TOP_K`, `RAG_SIMILARITY_THRESHOLD`, `RAG_RETRIEVAL_MODE`,
candidate counts, `RAG_RRF_K` и веса берутся из `Settings`. Handler не задаёт
собственных retrieval-порогов. Supabase RPC уже фильтрует только активные
knowledge documents. README и служебные файлы в индекс не входят.

Процесс не загружает embeddings или всю базу знаний в память: на каждый вопрос
создаётся один query embedding, а чанки возвращаются Supabase RPC. Один
Supabase client и один OpenAI client переиспользуются extraction, embedding и
answer providers, при этом prompt contracts остаются раздельными.

Query plan нормализует разделители тысяч и масштабы (`180 000`, `180.000`,
`180 тыс.`), сохраняет точные фразы статусов и добавляет только ограниченную
терминологию для согласования, срочности, статусов, перевозки и смешанных
категорий. Original, concise factual и terminology-expanded варианты каждый
проходят через существующий hybrid retrieval. Их позиции объединяются вторым
position-based RRF с тем же `RAG_RRF_K`; raw scores не складываются.

Generated `search_vector` migration 003 уже включает `document_title`,
`section_path`, `heading` и `content`, поэтому изменение индекса или
переиндексация для этой доработки не нужны.

## Grounding

Structured DTO ответа содержит текст, проверяемые claims, флаг недостаточного
контекста и противоречие источников. Каждый claim содержит собственные
`cited_chunk_ids`, должен входить в итоговый answer и подтверждаться текстом
источника. Service принимает только реально переданные чанки, проверяет
релевантность claim типу вопроса, конкретные числовые значения и длинное
дословное копирование.

Тип документа переводится в `normative`, `instruction`, `faq`, `example` или
`template`. Для обычного вопроса examples/templates удаляются из контекста,
если найден подтверждающий нормативный или инструктивный чанк. Конкретное
значение, отсутствующее в вопросе, допустимо только если оно присутствует в
цитируемом не-example источнике. Поэтому частный пример перевозки из `kb-011`
не может превратить 12 паллет пользователя в 4 тонны, маршрут через Химки,
дату, гидроборт или бюджет примера.

Суммы и относительные сроки приводятся к единому виду при подготовке вопроса и
проверке ответа. Пробел, NBSP, точка-разделитель тысяч и обозначение рублей не
меняют сумму. «Две недели» считаются как 14 дней без вычисления календарной
даты. При выводе по порогу приложение, а не LLM, детерминированно проверяет
принадлежность суммы нормативному диапазону и сравнивает длительность с
процитированным нормативным сроком. Пользователь получает естественное
объяснение, например: «До мероприятия осталось две недели, то есть 14 дней.
Это меньше нормативного срока в 30 календарных дней…».

При отсутствии контекста service отказывается угадывать. Внутренние reason code
различают `no_chunks`, `no_relevant_normative_chunks`, `unsupported_answer`,
`ambiguous_question`, `provider_unavailable` и `malformed_output`. Diagnostic
script дополнительно показывает `below_threshold`. Ошибки retrieval, timeout
или malformed provider response дают безопасный статус. В логах
остаются только fingerprint сообщения, статус, числа чанков/источников,
duration и безопасный error code; вопрос, context, prompt и raw response не
логируются.

## Изоляция диалога

`dialog_states.current_intent` хранит `idle`, `intake` или `regulation_qa`.
Переход в Q&A не меняет `active_request_id` и canonical draft. В этом режиме
Telegram adapter возвращает управление до вызова deterministic parser,
structured intake extraction, completeness и lifecycle. В intake RAG не
вызывается.

Повторный Telegram message использует существующий idempotency namespace в
`message_logs`. Полный вопрос и открытые Telegram ID туда не записываются.

## Evaluation

`data/evaluation/regulation_qa_cases.json` содержит regression-набор вопросов: прямые факты,
суммовые пороги, условный маршрут, обязательные поля, статусы, срочность,
смешанные категории, ответственность, неоднозначность и вопрос вне базы.

Offline-команда без внешних API:

```powershell
python scripts/evaluate_regulation_qa.py --offline --top-k 5 --show-failures
```

Она считает hit@k, MRR, source document accuracy и структурные показатели
grounding/refusal/citations, `answer_relevance`,
`unsupported_concrete_value_rate`, `example_leakage_rate` и
`normative_source_accuracy`. Эти показатели проверяют retrieval context и
validation contract. `answer_correctness_proxy` не является автоматической
semantic correctness текста; для неё нужен отдельный разрешённый model run и
ручная оценка.

Read-only диагностика реального Supabase retrieval:

```powershell
python scripts/debug_regulation_retrieval.py "Вопрос по регламенту"
```

Скрипт показывает нормализацию, strict/text/broad представления, все три
варианта, semantic/lexical/hybrid кандидатов, позиции, scores, threshold
decision и финальные чанки. Он не содержит mutation-вызовов, но обращается к
реальной Supabase и создаёт query embeddings, поэтому запускается только после
явного разрешения.

Локальная диагностика post-generation validation:

```powershell
python scripts/debug_regulation_answer.py "Вопрос по регламенту"
```

Она показывает безопасную сводку структурированного результата: количество
утверждений, идентификаторы процитированных фрагментов, конкретные значения и
стабильный код сработавшего правила. Полный текст найденных фрагментов, сырой
ответ модели, prompt и секреты не выводятся. Вывод не попадает в production
journal. Команда передаёт вопрос и выбранные нормативные фрагменты в OpenAI
answer provider и поэтому требует отдельного явного разрешения.

## Результат end-to-end smoke test

Regulation Q&A подключён к Telegram и проверен на реальном read-only retrieval.
Форматы `530000 руб` и `530 000 рублей` дают одинаковое нормативное решение;
сроки «10 дней», «14 дней» и «две недели» одинаково сравниваются с правилом
срочности. Вопросы о перевозке 5 и 12 паллет используют значения пользователя,
а не реквизиты учебного примера. Вопрос о текущей минимальной цене поставщика
получает контролируемый отказ из-за отсутствия таких данных в базе. Для
успешных ответов источники соответствуют использованным правилам.

## Ограничения MVP

- «Мои заявки» показывает одну страницу из пяти записей.
- Q&A не выбирает поставщика и не использует внешние данные.
- Нет reranker. Принадлежность суммы диапазону и сравнение длительности с
  нормативом вычисляются детерминированным post-processing, а не средствами
  RAG или LLM.
- При отсутствии OpenAI конфигурации intake может работать в `rule`, но Q&A
  корректно сообщает о временной недоступности.
- Ответы ограничены содержимым индексированной учебной базы и не используют
  актуальные цены, остатки или внешние каталоги поставщиков.
