# Evaluation Telegram intake extraction

Holdout `data/evaluation/telegram_intake_holdout.json` отделён от production-
словарей и regex. Он содержит 41 сценарий товаров, услуг, смысловых контрастов,
нескольких чисел/объектов, словесных и относительных дат, максимальной и
приблизительной суммы, периодической оплаты, неизвестного бюджета, неполного
описания и пользовательской самокоррекции. Дополнительные группы покрывают
словесные quantity, вывод `шт.`, measure/packaging units, capacity одной
позиции, relative deadline внутри сообщения, frequency/duration без deadline и
несколько независимых товарных или сервисных объектов.

Каждый кейс хранит `input`, компактный существующий `context`, ожидаемые поля и
null-поля, допустимые варианты текстовых полей, ожидаемое missing/next question,
critical fields и scenario tags. `expected_next_question=null` означает, что
конкретный вопрос для этого extraction-кейса не оценивается; строка `none`
означает ожидаемую готовность без следующего вопроса.

Запуск локального fallback без сети:

```powershell
python scripts/evaluate_telegram_extraction.py --mode rule --show-failures
```

Ручные платные запуски выполняются только после явного разрешения:

```powershell
python scripts/evaluate_telegram_extraction.py --mode openai --show-failures
python scripts/evaluate_telegram_extraction.py --mode hybrid --show-failures
```

Скрипт считает accuracy типа и категории, field precision/recall, exact match
critical fields, quantity, unit, суммы, даты и бюджета, accuracy решения о полноте, доли лишних
и пропущенных вопросов, пропущенных и галлюцинированных полей. Отдельно
выводятся hallucination rates для quantity, unit и date. Текст сравнивается
после безопасной нормализации регистра, `ё/е` и пробелов либо с перечисленными
допустимыми вариантами; смысловые расхождения не скрываются.
Допустимые падежные формы и равнозначные уточнённые формулировки перечисляются
как alternatives либо принимаются безопасным морфологическим сравнением.
Диагностика mismatch различает `semantic_error`, `missing_field`,
`hallucination` и `workflow_error`; нормализационные и допустимые варианты не
считаются ошибками.
Краткое `item_name` услуги допустимо, если ключевые системы или объём сохранены
в `specifications`: например, «разработка интеграции» вместе с «CRM с
телефонией». «Уборка офиса» и «клининг офиса» считаются равнозначными
пользовательскими названиями и не требуют изменения production output.

Полнота и порядок вопросов оцениваются отдельно. `missing_fields_correctness`
проверяет ожидаемые обязательные пропуски и не допускает, чтобы уже ожидаемое
заполненное поле попало в missing. `next_question_validity_accuracy` проверяет,
что вопрос относится к реально отсутствующему или невалидному полю.
`completeness_decision_accuracy` сравнивает само решение «спрашивать / не
спрашивать», а `next_question_order_exact_match` отдельно фиксирует совпадение
приоритетного порядка. Поэтому другой допустимый missing field не считается
ошибкой полноты, но остаётся видимым как `question_order_difference`.

Надёжность provider выводится отдельно: `provider_call_count`,
`provider_success_count`, `provider_failure_count`,
`evidence_validation_failure_count`, `fallback_count` и `fallback_rate`.
Каждый кейс изолирован. В `openai` ошибка provider остаётся ошибкой кейса без
подмены локальным результатом; в `hybrid` общий с Telegram application service
применяет deterministic fallback, после чего quality metrics считаются по
фактическому fallback-результату. Ошибочный кейс не исключается из знаменателей.
`--show-failures` добавляет `case_id`, stage, безопасный класс/код причины и
признак fallback, но не печатает request payload, prompt или сырой ответ.

Evidence failure не повторяется: это семантически невалидный уже распарсенный
ответ, а повтор того же запроса без корректирующего контекста лишь увеличил бы
стоимость. Настроенные retries provider (по умолчанию два повтора, максимум три
попытки) остаются только для rate limit, timeout/network и временной серверной
ошибки; evaluator дополнительных retries не создаёт.

Целевые пороги относятся к реальному `hybrid`: type, category, amount, date и
budget не ниже 0.95, critical exact match не ниже OpenAI baseline 0.929,
completeness не ниже 0.667 и ноль галлюцинированных полей. После успешного
реального quality gate режим по умолчанию — `hybrid` при наличии ключа и `rule`
без ключа; явная настройка по-прежнему имеет приоритет. Результаты ниже порога публикуются как есть; holdout не
используется для добавления частных regex. Автоматические regression tests
прогоняют fake structured provider и не обращаются к OpenAI, Telegram или
Supabase.
