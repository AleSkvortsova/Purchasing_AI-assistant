# Индексация и семантический поиск по базе знаний

На этом этапе реализованы загрузка подготовленных JSON, embeddings,
идемпотентная индексация в Supabase/pgvector и возврат релевантных чанков.
Поверх индекса реализованы semantic, lexical и hybrid retrieval. Генерация
ответа, reranker, история диалога и Telegram в этот контур не входят.

## Источник данных

Индексатор читает только результаты `scripts/prepare_knowledge_base.py`:

- `data/processed/knowledge_documents.json`;
- `data/processed/knowledge_chunks.json`.

Перед записью проверяются структура данных, уникальность идентификаторов и
ссылки чанков на документы. Стабильный UUID чанка и `content_sha256`
формируются на этапе подготовки базы знаний.

## Алгоритм индексации

1. Прочитать и проверить оба JSON-файла.
2. Upsert документов по `document_id`.
3. Upsert чанков по логическому уникальному ключу
   `(document_id, chunk_index)`, передавая актуальный UUID в поле `id`.
4. Найти чанки без embedding, с изменившимся `content_sha256` или другой
   моделью embedding.
5. Отправить только такие тексты в OpenAI Embeddings API пакетами
   (`EMBEDDING_BATCH_SIZE`, по умолчанию 50).
6. Проверить число и размерность полученных векторов.
7. Сохранить векторы и имя модели.
8. После полного успеха удалить устаревшие чанки по точному списку UUID.

Неизменившиеся чанки повторно не отправляются в OpenAI. Ключ
`--force-reembed` принудительно пересчитывает все векторы. Удаление можно
отключить ключом `--skip-delete`.

UUID чанка зависит от содержимого. Поэтому при изменении текста новый UUID
конфликтовал бы со старой строкой по `(document_id, chunk_index)`, если
вставлять по `id`. Индексатор использует этот логический unique constraint как
цель upsert: PostgreSQL атомарно обновляет строку, включая `id`, текст и
metadata. Для заменённой строки embedding сбрасывается и рассчитывается заново.
Остальные актуальные позиции не затрагиваются.

Supabase REST-клиент не объединяет все шаги в одну транзакцию. Поэтому при
ошибке возможны уже выполненные upsert документов или чанков, но очистка
устаревших данных не запускается. Пустой набор актуальных UUID также не может
инициировать массовое удаление.

## Подготовка схемы

Выполните вручную в Supabase SQL Editor:

1. `scripts/sql/001_initial_schema.sql`;
2. `scripts/sql/002_knowledge_base_vector.sql`;
3. `scripts/sql/003_hybrid_knowledge_search.sql`;
4. `scripts/sql/004_improve_lexical_retrieval.sql`.

Вторая миграция подключает `vector`, создаёт таблицы базы знаний, частичный
HNSW-индекс для cosine distance и RPC `match_knowledge_chunks`. Приложение
самостоятельно миграции не выполняет.

## Конфигурация

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-backend-only-service-role-key
OPENAI_API_KEY=your-openai-api-key
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_BATCH_SIZE=50
RAG_TOP_K=5
RAG_SIMILARITY_THRESHOLD=0.0
RAG_RETRIEVAL_MODE=hybrid
RAG_SEMANTIC_CANDIDATE_COUNT=20
RAG_LEXICAL_CANDIDATE_COUNT=20
RAG_RRF_K=60
RAG_SEMANTIC_WEIGHT=1.0
RAG_LEXICAL_WEIGHT=1.0
ENABLE_RAG_INDEX_ENDPOINT=false
```

Секреты нельзя передавать клиенту, возвращать через API, писать в логи или
коммитить. Значение `EMBEDDING_DIMENSIONS` должно совпадать с
`vector(1536)` в SQL-схеме.

## Команды

Безопасная локальная проверка файлов без создания внешних клиентов, сетевых
вызовов и записи в базу:

```powershell
python scripts/index_knowledge_base.py --dry-run
```

Реальная индексация вызывает платный OpenAI API и пишет в Supabase:

```powershell
python scripts/index_knowledge_base.py
python scripts/index_knowledge_base.py --force-reembed
python scripts/index_knowledge_base.py --batch-size 25 --skip-delete
```

Поиск возвращает чанки, а не сгенерированный ответ:

```powershell
python scripts/search_knowledge_base.py "как оформить закупку мониторов" --mode hybrid
python scripts/search_knowledge_base.py "матрица согласования" --mode lexical
python scripts/search_knowledge_base.py "договор" --top-k 3 --doc-type instruction
python scripts/search_knowledge_base.py "ошибка" --show-content
```

## API

- `GET /api/v1/rag/health` — состояние конфигурации и статистика индекса без
  вызова OpenAI;
- `POST /api/v1/rag/search` — embedding запроса и поиск релевантных чанков;
- `POST /api/v1/rag/index` — индексация, по умолчанию скрыта ответом 404 и
  включается только через `ENABLE_RAG_INDEX_ENDPOINT=true`.

Поиск ограничивает `top_k` диапазоном 1–20, отклоняет пустой запрос,
поддерживает порог similarity и фильтр по типам документов. В ответе есть
идентификаторы, источник, заголовки, текст чанка и similarity; LLM-ответ не
генерируется.

Подробности FTS, RRF и evaluation описаны в
[`hybrid_retrieval.md`](hybrid_retrieval.md).
