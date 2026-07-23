# Гибридный поиск по базе знаний

## Зачем нужен hybrid retrieval

Embeddings хорошо находят смысловые совпадения, но точный нормативный источник
может уступать общим тематическим документам. Например, semantic search для
вопроса «Кто согласует закупку на 180000 рублей?» находил раздел
`kb-009 > Матрица согласования` только за пределами top-5.

Основной режим retrieval теперь объединяет:

- semantic search по cosine similarity;
- PostgreSQL full-text search;
- Reciprocal Rank Fusion (RRF) по позициям кандидатов.

Генерация ответа и применение найденного правила не входят в этот этап.

## Full-text search

Миграция `scripts/sql/003_hybrid_knowledge_search.sql` проверяет наличие
конфигурации `russian` в `pg_ts_config`. В стандартном PostgreSQL используется
`pg_catalog.russian`; если она недоступна, миграция выбирает `simple` и выводит
`NOTICE`. Английская конфигурация не используется.

Generated stored column `knowledge_chunks.search_vector` включает:

- `document_title`, `section_path`, `heading` с весом A;
- `content` с весом B;
- дополнительное нормализованное представление с весом B.

GIN-индекс ускоряет поиск. RPC `match_knowledge_chunks_lexical` применяет
`websearch_to_tsquery`, фильтры активного документа и типа, затем сортирует по
`ts_rank_cd`, приоритету и номеру чанка.

После реальной проверки добавлена миграция
`scripts/sql/004_improve_lexical_retrieval.sql`. Она не меняет таблицы,
`search_vector` или embeddings, а через `create or replace` улучшает
формирование lexical candidates.

Для одного вопроса строятся три представления:

- `strict_query` — полный нормализованный запрос, включая числа;
- `text_query` — запрос без отдельных чисел, валюты и ограниченного списка
  вопросительных/служебных слов;
- `broad_query` — значимые лексемы `text_query`, объединённые через OR и
  допускающие prefix-match для близких словоформ.

Кандидат принимается при совпадении хотя бы с одним представлением. Итоговый
lexical score рассчитывается как `strict rank × 3 + text rank × 2 +
broad rank × 1`. Этот score сортирует только lexical candidate list. Hybrid
по-прежнему применяет RRF к позициям и не складывает raw scores.

## Нормализация чисел

Нормализация:

- приводит текст к нижнему регистру;
- заменяет неразрывные пробелы обычными;
- удаляет пробелы между цифрами;
- приводит длинные тире к дефису.

Поэтому `180000`, `180 000` и `180 000` получают общий дополнительный токен
`180000`. Исходный текст также индексируется для обычного FTS.

Для text-only и broad поиска отдельные числовые токены и формы `руб`,
`рубль`, `рубля`, `рубли`, `рублей` удаляются. Также исключается небольшой
фиксированный список: `кто`, `что`, `какой`, `какая`, `какие`, `можно`, `ли`,
`на`. Значимые процессные термины, включая «согласование», «закупка»,
«бюджет», «статус» и «поставщик», сохраняются.

Например:

```text
Кто согласует закупку на 180000 рублей?
→ согласует закупку
```

Число не должно быть обязательным для всех lexical candidates: нормативная
матрица содержит диапазон `100001–500000`, но не конкретное значение
`180000`. Broad OR позволяет матрице войти в candidate pool по значимым
терминам.

Retrieval не определяет, входит ли `180000` в диапазон `100 001–500 000`.
Такое вычисление относится к будущему структурированному rule engine.

## Reciprocal Rank Fusion

Semantic и lexical поиск формируют независимые candidate pools. Для каждого
чанка используются позиции, а не несопоставимые raw cosine/`ts_rank`:

```text
semantic_rrf = semantic_weight / (rrf_k + semantic_rank)
lexical_rrf  = lexical_weight  / (rrf_k + lexical_rank)
hybrid_score = semantic_rrf + lexical_rrf
```

Чанк может присутствовать в одном или обоих списках. Объединение выполняется
по `chunk_id`, поэтому дублей нет. По умолчанию:

- semantic candidates: 20;
- lexical candidates: 20;
- `rrf_k`: 60;
- оба веса: 1.0;
- итоговый top-k: 5.

Диагностический результат содержит `similarity`, `lexical_score`,
`semantic_rank`, `lexical_rank` и `hybrid_score`. Raw scores показываются для
диагностики, но напрямую не складываются.

## Режимы и CLI

```powershell
python scripts/search_knowledge_base.py `
  "Кто согласует закупку на 180000 рублей?" `
  --mode hybrid `
  --top-k 5 `
  --debug-scores

python scripts/search_knowledge_base.py "Матрица согласования" --mode lexical
python scripts/search_knowledge_base.py "правила бюджета" --mode semantic
```

Lexical mode не вызывает OpenAI. Semantic и hybrid создают один embedding
поискового запроса. Полный текст чанка выводится только с `--show-content`.

## Evaluation

`data/evaluation/retrieval_cases.json` содержит 15 контрольных вопросов с
реальными `document_id` и разделами. Скрипт считает:

- Hit@1, Hit@3, Hit@5;
- MRR;
- Preferred Hit@1, Hit@3, Hit@5;
- среднюю latency.

Сравнение реальных режимов:

```powershell
python scripts/evaluate_retrieval.py `
  --mode all `
  --top-k 5 `
  --show-failures
```

Архитектурная проверка без сети:

```powershell
python scripts/evaluate_retrieval.py --mode all --top-k 5 --offline
```

Offline-режим использует случайно-подобные детерминированные fake embeddings.
Его метрики подтверждают корректность контрактов и расчётов, но не качество
production retrieval.

## Ограничения

- FTS и embeddings не вычисляют числовые диапазоны.
- Нет rule engine и структурированной таблицы правил.
- Нет table-aware rechunking и reranker.
- Retrieval возвращает чанки, но не генерирует итоговый ответ.
- Веса RRF требуют проверки на реальном evaluation-наборе после миграции.
