# Документация проекта

Короткий навигатор по материалам проекта. Описание продукта и быстрый запуск
находятся в [корневом README](../README.md).

## `final/` — итоговый комплект

**Аудитория:** проверяющий, владелец продукта, участники защиты.
**Назначение:** пять самостоятельных документов, необходимых для понимания и
сдачи проекта.

- [Итоговое техническое задание](final/FINAL_TECHNICAL_SPECIFICATION.md)
- [Отчёт о приёмочном тестировании](final/ACCEPTANCE_TEST_REPORT.md)
- [Системные инструкции и программные правила](final/SYSTEM_PROMPTS_AND_RULES_FINAL.md)
- [Оценка стоимости и экономической эффективности](final/COST_ESTIMATE_FINAL.md)
- [Руководство по базе знаний](final/KNOWLEDGE_BASE_GUIDE.md)

## `guides/` — пользовательские документы

**Аудитория:** внутренний заказчик.
**Назначение:** работа с Telegram-ботом без эксплуатационных и инженерных
деталей.

- [Руководство пользователя](guides/USER_GUIDE.md)

## `operations/` — эксплуатация

**Аудитория:** администратор и инженер сопровождения.
**Назначение:** установка, конфигурация, проверки, обновление VPS, диагностика и
восстановление.

- [Руководство администратора](operations/ADMIN_GUIDE.md)

## `technical/` — инженерная документация

**Аудитория:** разработчики и технические рецензенты.
**Назначение:** архитектура, контракты данных, extraction, intake, lifecycle,
правила согласования, индексация, retrieval и Telegram adapter.

- [Общая техническая архитектура](technical/TECHNICAL_ARCHITECTURE.md)
- [Схема базы данных](technical/database_schema.md)
- [Извлечение approval context](technical/approval_context_extraction.md)
- [Детерминированный rule engine](technical/approval_rule_engine.md)
- [Ядро intake-диалога](technical/intake_dialog_core.md)
- [Persistence orchestration](technical/intake_persistence_orchestration.md)
- [Lifecycle заявки](technical/request_lifecycle.md)
- [Подготовка базы знаний](technical/knowledge_base_preparation.md)
- [Индексация RAG](technical/rag_indexing.md)
- [Гибридный поиск](technical/hybrid_retrieval.md)
- [Regulation Q&A](technical/regulation_qa.md)
- [Telegram adapter](technical/telegram_adapter.md)
- [Оценка Telegram extraction](technical/telegram_extraction_evaluation.md)
- [Runbook migration 007](technical/intake_persistence_migration_runbook.md)
- [Runbook migrations 008/009](technical/request_lifecycle_migration_runbook.md)

## `planning/` — развитие

**Аудитория:** владелец продукта и команда разработки.
**Назначение:** полный перечень ограничений, технического долга и следующих
этапов после MVP.

- [Product backlog](planning/PRODUCT_BACKLOG.md)

## `project_management/` — управление сдачей

**Аудитория:** автор проекта и участники подготовки защиты.
**Назначение:** внутренний контроль комплектности материалов и готовности к
сдаче.

- [Чек-лист PEf05](project_management/PEF05_DELIVERY_CHECKLIST.md)

## `archive/` — история

**Аудитория:** команда сопровождения и аудиторы истории решений.
**Назначение:** завершённые milestone, исходная фиксация scope и снимки аудита;
они не описывают текущее состояние продукта.

- [Первоначальные границы MVP](archive/MVP_SCOPE.md)
- [Снимок аудита документации](archive/DOCUMENTATION_AUDIT.md)
- [Завершённый backend milestone](archive/backend_mvp_lifecycle_completed.md)

Исходные постановочные DOCX сохранены в `archive/source_materials/` как
исторические материалы, а не как равноправные финальные документы.

## Правило размещения

Итоговый согласованный материал помещается в `final/`; инструкция пользователя
— в `guides/`; runbook — в `operations/`; инженерное описание — в
`technical/`; планы — в `planning/`; внутренний контроль сдачи — в
`project_management/`; завершённый рабочий материал — в `archive/`.
