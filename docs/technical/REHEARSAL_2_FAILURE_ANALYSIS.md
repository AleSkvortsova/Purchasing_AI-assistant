# Root-cause analysis второго production rehearsal

Дата анализа: 11.08.2026

Release candidate: `4c398f23f6d3a2d35850ff3c39d0bdc963a8ecd7`

Статус: исторический RCA; follow-up P0-D/P0-F зафиксирован отдельно

Область проверки: текущий HEAD, локальные tests/evaluation,
`docs/technical/REHEARSAL_FAILURE_ANALYSIS.md` и факты, приведённые в задании
по второму rehearsal. Telegram, OpenAI, Supabase и другие внешние API не
вызывались.

## 1. Executive summary

Второй rehearsal подтвердил, что исправления CASE 1–5 работают в ряде
проверенных веток, но выявил четыре общих разрыва на границах компонентов:

1. режим диалога, активный intake draft и pending clarification являются
   разными осями состояния; повторный вход в уже активный Regulation Q&A не
   означает новый чистый сеанс;
2. deterministic и structured extraction объединяются несимметрично: точные
   числовые поля защищены, но `procurement_type` может исчезнуть при structured
   `null` или rejected result;
3. category resolution умеет сохранять варианты, но не различает сильные
   classifier candidates и общий fallback; semantic guard является positive
   validation только для предметов, которые классификатор уже знает;
4. короткий ответ на ожидаемое free-text поле валидируется синтаксически, но не
   проверяется на семантическую совместимость с полем.

Это не одна ошибка, однако большинство новых дефектов имеет общий
архитектурный знаменатель: решения на границе перехода принимаются по локальному
сигналу без явного provenance и без проверки полного контекста состояния.

Два дефекта являются P0 до следующей защиты:

- неизвестный предмет может получить слабый fallback категорий, произвольная
  категория того же G/S-префикса может стать confirmable и зарегистрироваться;
- семантически несовместимый ответ может заполнить высокозначимое обязательное
  поле (`department`) и попасть в зарегистрированную карточку без
  подтверждения.

Перекрёстное загрязнение `regulation_qa`/pending и потеря service type в hybrid
merge — P1: они ломают основной пользовательский путь, но в наблюдённых
сценариях приводят к уточнению, а не к тихой регистрации заведомо неверной
категории.

### Ограничение доказательности

В рабочем дереве нет полного production transcript, полного фрагмента
`journalctl` и snapshot `dialog_states.state_data` для описанных сообщений.
Доступны только выдержки из задания. Поэтому ниже строго разделены:

- **доказано** — следует из текущего кода и приведённых результатов/логов;
- **наиболее вероятно** — единственный или наиболее согласованный с фактами
  путь, но для него не хватает production snapshot;
- **нельзя доказать** — нужны безопасные state/decision diagnostics конкретного
  production turn.

## 2. Что подтвердилось как исправленное после первого rehearsal

По фактам второго rehearsal подтверждены следующие рабочие ветки:

- после явного `MENU_NEW` полный запрос про стеллажи извлекается корректно;
- суммы `примерно 36 000 рублей` и `не более 75 000 рублей` распознаются;
- mixed goods + service для вентиляторов обнаружен и представлен двумя
  потребностями;
- очевидный OOD-вопрос о фильме получает controlled refusal;
- вопрос о срочной закупке получает релевантный нормативный ответ;
- по предоставленной выдержке `journalctl` нет признаков инфраструктурных
  HTTP/OpenAI/Supabase ошибок.

Это ограничивает scope: не требуется переписывать amount normalization,
базовый mixed detector, очевидный OOD gate, urgency retrieval или
инфраструктурные клиенты.

### Follow-up status P0-D/P0-F

11.08.2026 выполнен ограниченный цикл стабилизации двух P0:

- P0-D закрыт positive-support invariant: category candidates теперь хранят
  source, `selectable` и `readiness_eligible`; generic fallback является weak и
  не показывается как подходящий вариант; неизвестный предмет остаётся с
  unresolved/invalid category, а readiness и lifecycle confirm повторно
  блокируют регистрацию;
- P0-F закрыт deterministic guard для high-risk awaiting fields. Очевидные
  purpose/location/date ответы не записываются в `department`, аналогичная
  ограниченная защита действует для contact person и delivery location;
  mismatch не передаётся structured extractor и не сохраняется;
- SQL, migrations, knowledge base, Regulation Q&A, `/start`, unit lexicon и
  остальные P1/P2 этого документа не менялись.

Исторические traces и формулировки причин ниже сохранены как состояние до
исправления.

### Follow-up status P1 A/H, A, B, G1

11.08.2026 выполнен следующий ограниченный цикл стабилизации:

- явный вход в Regulation Q&A теперь очищает старый pending, а `/start`
  переводит диалог в `idle`; активный intake draft в обоих случаях сохраняется;
- cancellation использует action/target contract без фиксированного расстояния
  между словами и не смешивается с объяснением статуса «Отменена»;
- domain gate различает явное личное и организационное назначение, конфликт
  назначений приводит к controlled clarification до retrieval;
- explicit deterministic `procurement_type` с evidence сохраняется при
  structured null/rejection/fallback, а противоположный structured type не
  перезаписывает его молча.

P0 category/awaiting guards и остальные P2 этого документа не менялись.

## 3. Таблица новых дефектов

| ID | Severity | Наблюдаемое поведение | Root cause | Тип | Затронутые модули | Test gap |
|---|---|---|---|---|---|---|
| A | P1 | В Regulation Q&A вопрос об отмене превращается в уточнение предмета/типа; повтор даёт `repeated_clarification` | cancellation regex не распознаёт длинную вставку; `MENU_REGULATIONS` не очищает pending при повторном входе в тот же mode | CASE 4–5 generalization + скрытый state-transition gap | `app/rag/question_understanding.py`, `app/rag/conversation.py`, `app/bot/dialog_modes.py`, `app/bot/adapter.py` | нет реального cancellation paraphrase вместе с re-entry и persistent pending |
| B | P1 | Личная покупка холодильника получает внутренний brand-policy answer | brand intent автоматически означает `known_domain_intent`; личная/организационная цель в модели отсутствует | скрытый domain-model gap | `app/rag/question_understanding.py`, `app/rag/answering.py` | нет contrast tests personal home vs office use |
| C | P1 | Выбор mixed-позиции принимается только почти точной формой | отдельный `_selected_item()` использует односторонний substring match без морфологии/номера | скрытый UX/parser gap | `app/bot/adapter.py` | happy paths используют короткие канонические labels/actions |
| D | **P0** | Вентилятор получает G01–G04, G02 проходит confirm и регистрацию | общий fallback G01–G04 без score/provenance; при classifier=`none` semantic guard не доказывает совместимость | неполное обобщение CASE 3 | `app/bot/adapter.py`, `app/bot/categories.py`, `app/intake/validators.py`, lifecycle readiness | negative tests только для известных classifier mismatches |
| E | P2 | `пар/пары` не принимаются как unit | формы отсутствуют в canonical unit registry | ожидаемое ограничение словаря | `app/bot/normalization.py`, `app/bot/parser.py` | нет business-unit lexicon matrix |
| F | **P0** | «для сотрудников сборочного участка» записывается как department | awaiting free text после trim принимается без semantic compatibility | скрытый clarification-boundary gap | `app/bot/parser.py`, `app/intake/validators.py`, extraction evidence checks | проверяется нормализация, не итоговый смысл ответа |
| G1 | P1 | «профилактическое обслуживание» всё равно требует типа | deterministic extractor распознаёт service, но `procurement_type` не защищён при hybrid merge; конкретный structured result неизвестен | merge regression/generalization gap | `app/bot/extraction.py`, `app/extraction/intake.py`, `app/bot/adapter.py` | provider mocks всегда возвращают подтверждённый service type |
| G2 | P2 | После category повторяется тот же UX-вопрос о результате/требованиях | обязательные `description` и `specifications` имеют одинаковый presentation text | UX contract gap, не доказанный persistence regression | `app/intake/field_registry.py`, `app/bot/formatters.py` | тесты проверяют field codes, не различимость двух последовательных вопросов |
| H | P1 | Полный текст после `/start` попадает в clarification; после `MENU_NEW` работает | `/start` не меняет persisted mode/pending; stale Regulation context может пережить приветствие | тот же скрытый state-transition gap, что A | `app/bot/adapter.py`, `app/bot/handlers.py`, `app/bot/dialog_modes.py` | idle first-text проверен, persisted non-idle `/start` — нет |

## 4. Подробный trace CASE A–H

### 4.1. CASE A — Regulation Q&A и отмена

#### Доказанный путь

1. `TelegramIntakeAdapter.handle_menu(MENU_REGULATIONS)` вызывает
   `set_mode(user_id, "regulation_qa")`.
2. Следующий `handle_text()` проверяет mode до active intake draft. При
   `regulation_qa` он немедленно вызывает `_handle_regulation_question()`.
   Следовательно, приведённые `mode=regulation_qa` и
   `retrieval_status=not_called` соответствуют RAG conversation path, а не
   intake parser.
3. Локальная deterministic классификация фразы
   «Можно ли отменить уже начатую, но ещё не отправленную заявку?» даёт:
   `primary_intent=ambiguous_followup`,
   `missing_required_context=(purchase_subject, purchase_type)`.
4. Причина: `_asks_for_cancellation()` допускает не более 20 символов между
   формой `отмен...` и `заяв...`. Вставка «уже начатую, но ещё не отправленную»
   длиннее. Другой cancellation pattern не совпадает.
5. Слово `заявку` является положительным procurement signal. Поэтому вопрос не
   становится `outside_domain`, а падает в общий `ambiguous_followup`.
6. `_clarification()` для такого intent требует предмет и тип и генерирует
   наблюдавшийся текст. Это технически последовательный результат ошибочной
   классификации, но бизнес-смысл вопроса об отмене этих slots не требует.
7. Pending сохраняет original question, primary/secondary intents, known/missing
   slots, clarification step и fingerprint в
   `dialog_states.state_data.regulation_pending_clarification`.
8. И in-memory, и Supabase реализации `set_mode()` очищают pending только если
   новый mode **не** `regulation_qa`. Повторное нажатие той же кнопки обновляет
   mode, но не создаёт clean Regulation session.
9. Повторный вопрос имеет тот же ошибочный intent и не заполняет ожидаемые
   purchase slots. Он не считается новым независимым вопросом, fingerprint
   уточнения повторяется.
10. `reason_code=repeated_clarification` означает срабатывание защиты от цикла в
    `answer_regulation_turn()`: retrieval намеренно не вызывается, pending после
    результата очищается. Это не OpenAI/retrieval failure.

#### Ответы на вопросы CASE A

- Оба сообщения при приведённом `mode=regulation_qa` проходили Regulation path;
  active draft не мог перехватить их, потому что mode check выполняется раньше.
- Primary intent — `ambiguous_followup`, не `request_cancellation`.
- Missing slots — `purchase_subject` и `purchase_type`.
- Конфликт не между cancellation и intake semantics: cancellation вообще не
  был распознан. Конфликт возник между ошибочным ambiguous intent и сохранённым
  regulation pending context.
- Старый pending действительно может пережить повторный вход через тот же menu.
- Существующий navigation test доказывает изоляцию active draft от Regulation
  path, но использует `FakeRegulationService` и не проверяет semantic
  classification. Он не покрывает цепочку completed/active intake → menu →
  cancellation paraphrase → re-entry.

#### Наиболее вероятно

Первый turn создал pending с двумя missing slots; второй вход в menu сохранил
его, а повтор фразы активировал loop guard. Это полностью совпадает с кодом и
`repeated_clarification`.

#### Нельзя доказать без snapshot

Нельзя подтвердить exact `state_data` до первого menu click, был ли там ещё
более старый pending, и какие `active_request_id/current_intent` были записаны
перед последовательностью. Для непосредственной причины первого ответа это не
нужно, но для восстановления полной истории state transition — нужно.

**Классификация:** частично regression CASE 4–5 (cancellation paraphrase не
обобщился), частично ранее непокрытый re-entry state-transition defect.

### 4.2. CASE B — личная покупка проходит domain gate

#### Доказано

1. Фраза «Какую марку лучше выбрать?» совпадает с brand regex
   `бренд|марк|эквивалент` и получает primary
   `brand_equivalent_policy`.
2. `_domain_decision()` возвращает `known_domain_intent` для любого известного
   intent. Наличие внутреннего организационного контекста отдельно не
   проверяется.
3. В `RegulationQuestionUnderstanding` нет полей personal purpose,
   organisational context или audience. Маркеры `себе`, `домой`, `для дома`,
   `лично`, `для семьи`, `в квартиру` не распознаются.
4. Здесь decisive signal — `марка`, а не общий глагол `купить`: brand intent сам
   обходит ambiguous/outside gate. Procurement vocabulary дополнительно делает
   near-domain формулировки более вероятными, но не является единственной
   причиной этого результата.
5. Final intent validator для brand policy проверяет в claim слова
   `бренд|эквивалент|референс|обоснование`. Корпоративный brand answer проходит,
   потому что он отвечает выбранному intent; личная цель повторно не
   проверяется.

#### Наиболее вероятно

Нормативный FAQ chunk про бренд прошёл retrieval и claim validation именно из-за
brand intent. Это согласуется с указанным пользователем источником.

#### Нельзя доказать без retrieval diagnostics

Нельзя восстановить все candidate chunks, ranks и выбранные chunk IDs
конкретного production turn. Это не меняет доказанную ошибку domain gate.

#### Общий безопасный механизм

Нужен не blacklist холодильников, а двухсторонний domain decision:

- explicit personal-consumer purpose без организационного признака → controlled
  refusal до retrieval;
- explicit organisational purpose (`в офис`, `для подразделения`, `для
  сотрудников`, `на склад`, `для производства`) → internal procurement;
- одновременно personal и organisational либо ни одного надёжного признака →
  краткое clarification, если intent требует такого различения.

Contrast tests должны включать одну и ту же сущность: «холодильник домой» vs
«холодильник в офисную кухню», «ноутбук себе» vs «ноутбук сотруднику».

### 4.3. CASE C — выбор decomposition need

#### Доказано

Mixed detection создаёт отдельные `ProcurementItemCandidate`, но выбор обрабатывает
отдельная функция `_selected_item()`, а не category candidate parser.

Поддерживаются:

- вхождение полного сохранённого `item_name` в нормализованный ответ;
- короткий category label или полный category label;
- грубые type markers `товар/поставка` и `услуга/работа/монтаж`;
- специальные G05/S05 markers для лицензии/установки.

Не поддерживаются:

- выбор номером, несмотря на нумерацию в сообщении;
- лемматизация и морфологическое согласование;
- симметричный token overlap;
- удаление командных слов «давайте», «сначала», предлога `с`;
- безопасное сопоставление head noun к длинному candidate label.

Сравнение одностороннее: сохранённое `item_name` должно целиком находиться в
ответе. Поэтому `вентиляторы` не содержит `два промышленных вентилятора`, а
почти exact label содержит его. Это не тот механизм, где category names уже
поддерживают partial matching.

#### Можно ли унифицировать без fuzzy guessing

Да. Без общей нечёткой дистанции можно использовать один selection contract:
номер; exact normalized label; однозначный нормализованный category/type alias;
однозначное пересечение значимых токенов после удаления quantity и командных
слов; при нескольких matches — не выбирать. Морфологию достаточно ограничить
предметными окончаниями/лемматизацией, не применяя fuzzy matching к кодам,
суммам и числам.

#### Почему тесты дали ложную уверенность

Товарный multicategory test использует candidates `компьютер` и `стол`, а
ответы `компьютер`, `давайте компьютер`, `только компьютер`, «начнём с
компьютерной техники». Во всех вариантах канонический короткий token буквально
присутствует. Mixed test выбирает «Начнём с монтажа», что совпадает с service
marker. Длинный label с числом, прилагательным и другой падежной формой не
проверяется.

### 4.4. CASE D — небезопасный category candidate set

#### Доказанный trace

1. Для вентилятора в `DeterministicCategoryClassifier` нет keyword, natural
   alias или item pattern. В частности, G02 знает мебельные stems
   `крес/мебел/шкаф/стол/тумб`, но не вентилятор.
2. Если derived classification не даёт exact/candidates,
   `_fallback_category_candidates()` возвращает для goods ровно
   `(G01, G02, G03, G04)`. Это точный источник наблюдавшегося списка.
3. Эти четыре значения не являются classifier candidates. Это generic fallback
   «первые категории» без предметной связи.
4. `CategoryCandidateOption` хранит только `code` и `label`. Confidence, score,
   provenance (`classifier`/`fallback`) и eligibility в state не сохраняются.
   Поэтому ответ на вопрос о confidence каждого варианта: **confidence/score не
   вычислялись и равны не “низким числам”, а отсутствуют в модели**.
5. Parser принимает номер/код/name только внутри показанного списка. Membership
   доказывает лишь, что пользователь выбрал показанный вариант, а не его
   семантическую совместимость.
6. `validate_draft()` сначала проверяет G/S-префикс. Затем повторно запускает
   classifier по draft. Ошибка возникает, если classifier дал другой exact
   result либо non-empty candidates без выбранного кода.
7. Для неизвестного предмета classifier возвращает `none`; обе ветки mismatch
   false. Следовательно, G02 проходит.
8. Lifecycle повторно использует ту же readiness/validation модель. Отдельного
   semantic proof на confirm нет, поэтому ошибка доходит до регистрации.

#### Ответ о характере guard

Guard является частичной positive compatibility validation для закрытого
словаря известных предметов и negative mismatch check для остального. Он не
доказывает совместимость неизвестного предмета с выбранной same-prefix
категорией. Да, аналогичная arbitrary same-prefix category может пройти, если
классификатор возвращает `none` и нет другого специального правила.

#### Почему negative tests не обобщились

Светильники, стол и ноутбук дают classifier exact/candidates, отличные от
переданной категории. Поэтому тесты попадают в активную mismatch-ветку.
Вентилятор возвращает `none`, то есть тестирует отсутствующую ветку
`unknown subject + arbitrary same-prefix category`.

#### Безопасное поведение

Если нет strong exact/candidate support, безопасный итог — unresolved category
и содержательное уточнение/эскалация, а не общий список G01–G04. Слабый fallback
не должен участвовать в readiness как доказательство совместимости.

#### Нельзя доказать без snapshot

Нельзя подтвердить, на каком turn generic candidates были сохранены и сколько
раз перечитывались из persistence. Их происхождение по составу списка, однако,
однозначно подтверждается кодом.

### 4.5. CASE E — unit `пар/пары`

#### Доказано

Canonical `_UNIT_ALIASES` поддерживает:

- `шт.`/штуки/единицы;
- кг;
- литры;
- метры;
- упаковки;
- пачки;
- коробки;
- комплекты;
- м²;
- часы;
- дни;
- услуги.

Форм `пара/пары/пар` в реестре нет. Awaiting `unit` вызывает exact
`normalize_unit()` и controlled `Unsupported unit`, преобразованный в
пользовательскую подсказку.

Новый CASE 1 invariant сработал правильно: explicit unsupported unit не был
автоматически заменён `шт.`. Значение `шт.` появилось только после явного ответа
пользователя «штуки». Это словарное ограничение, а не новый архитектурный
дефект.

К очевидным кандидатам на согласованный business lexicon относятся пары,
рулоны, листы, наборы, мешки, бутылки/канистры и тонны; их окончательный список
должен исходить из фактических закупочных данных/владельца процесса, а не из
случайного расширения regex. Для MVP разумен фиксированный версионируемый
лексикон с морфологическими формами и regression matrix.

### 4.6. CASE F — ответ записан не в то поле

#### Доказано

1. При active `awaiting_field_code=department` parser игнорирует свободное
   извлечение и разбирает весь ответ как значение именно department.
2. Для free-text fields `_parse_value()` возвращает исходную строку.
3. `IntakeFieldValidator.normalize()` для полей без специального validator
   делает только trim/empty check и возвращает строку.
4. Поэтому «для сотрудников сборочного участка» является валидным department
   по текущему контракту; предлог/цель могут быть лишь механически очищены в
   отображении, но смысл не проверяется.
5. Та же проблема относится к `delivery_location`, `business_justification`,
   `specifications`, `desired_result`, `description`, `item_name`, contact и
   другим free-text fields: короткий ответ может быть записан в ожидаемое поле,
   даже если явно похож на другое.
6. Structured extraction имеет confidence/evidence gate:
   `_trusted()` проверяет evidence against source и отклоняет неподтверждённые
   field candidates. Direct clarification reply этот gate не использует.

#### Наиболее вероятно

Formatter показал очищенное «сборочного участка», но canonical department был
заполнен тем же clarification turn и completeness посчитала его completed.

#### Нельзя доказать без snapshot

Точное сохранённое значение, `field_state.source` и итоговая регистрация этого
конкретного draft требуют snapshot/card. Сам путь ложного принятия доказан.

#### Scope будущего guard

Полная универсальная semantic model не обязательна. До защиты достаточно
ограниченного high-risk gate для `department`, `contact_person`, location и
полей, влияющих на readiness/routing: распознавать явные role/purpose prefixes,
не принимать очевидно несовместимое значение, предлагать correction или
подтверждение. Для specifications/desired result предпочтительнее различимый UX
и мягкое подтверждение, а не агрессивное автоматическое переназначение.

### 4.7. CASE G — service type и повтор вопроса

#### G1. Initial service intent

**Доказано:** deterministic extractor содержит stem `обслуживан...`; фраза
«профилактическое обслуживание» должна дать
`procurement_type=service`. Однако `merge_intake_candidates()` начинает со
structured values, а deterministic authority fields включают amount, quantity,
unit, budget status и date — но не `procurement_type`. Если structured type
равен `null` или был rejected evidence validation, deterministic service не
копируется в merged update. Результат остаётся без типа, и completeness задаёт
«Это товар или услуга?».

**Наиболее вероятно:** OpenAI result не содержал принятого `procurement_type`,
после чего merge потерял валидный deterministic candidate. Наблюдаемое
уточнение полностью соответствует этому пути.

**Нельзя доказать:** был ли structured type `null`, rejected по confidence,
rejected по evidence или provider целиком ушёл в fallback. Production log
показывает только field names/counts в зависимости от debug mode, а raw safe
decision trace конкретного turn не предоставлен.

#### G2. Повтор одинакового UX-вопроса

**Доказано:** для service `description` обязательно и имеет priority 30;
`category_code` — priority 40; `specifications` обязательно и имеет priority
60. Formatter отображает для **обоих** `description` и `specifications` один и
тот же текст: «Опишите, какой результат нужен и какие требования важны для
услуги.»

Следовательно, наиболее согласованный trace:

1. первый ответ заполнил `description`;
2. следующим missing стало `category_code`;
3. после выбора S01 следующим missing стало `specifications`;
4. formatter повторил тот же пользовательский вопрос для другого field code;
5. второй ответ заполнил specifications, и flow продолжился.

Для S01 `desired_result` не является обязательным: в registry он требуется
только для S11. Поэтому observed repeat не следует объяснять парой
`specifications/desired_result`.

**Нельзя доказать:** точные field states до/после category selection без
snapshot. Корректная итоговая карточка и продолжение после второго ответа
говорят против потери persistence, но не являются строгим доказательством.

**Классификация:** прежде всего неудачный UX contract двух разных полей, а не
подтверждённый persistence regression.

### 4.8. CASE H — текст сразу после `/start`

#### Доказано

1. `start_message()` только читает active draft и возвращает greeting/notice.
   Он не вызывает `set_mode()`, не очищает regulation pending и не создаёт
   intake session.
2. Если mode действительно `idle`, первый свободный текст обрабатывается как
   новый intake и может создать draft. Это закреплено
   `test_first_text_creates_minimal_update_and_returns_next_question`.
3. Если persisted mode остаётся `regulation_qa`, `handle_text()` отправляет
   текст в RAG до проверки active draft.
4. Если там же есть pending clarification, длинный текст сначала трактуется как
   ответ на missing slots, пока `_is_new_question()` не признает его новым
   полным вопросом.
5. `MENU_NEW` устанавливает mode=`intake`; любой переход в не-Regulation mode
   очищает regulation pending. Поэтому тот же текст после кнопки попадает в
   правильный intake path.

#### Наиболее вероятно

После `/start` сохранились `current_intent=regulation_qa` и/или pending из CASE
A. Первый запрос про стеллажи был интерпретирован как regulation clarification;
повтор активировал соседнюю повторную clarification ветку. Наблюдаемые тексты и
успех после `MENU_NEW` согласуются именно с этим.

#### Нельзя доказать без snapshot

Нельзя назвать фактические `current_intent`, `active_request_id`, pending
payload и TTL в момент `/start`. Поэтому нельзя утверждать, что это был именно
pending CASE A, а не другое сохранённое состояние.

#### UX-контракт

Текущий контракт неоднозначен: unit test разрешает `idle → text` как создание
intake, но `/start` не гарантирует idle. Пользователь воспринимает `/start` как
новое начало, а backend — как presentation-only command с сохранением режима.
Перед исправлением нужно явно выбрать один контракт: `/start` восстанавливает
текущий режим с явным сообщением либо сбрасывает conversational mode/pending,
не удаляя active draft.

## 5. State-machine analysis

Текущая модель фактически является произведением нескольких состояний, а не
одним автоматом:

| Ось | Значения | Хранилище/поведение |
|---|---|---|
| Dialog mode | `idle`, `intake`, `regulation_qa` | `dialog_states.current_intent` |
| Active request | отсутствует / draft request id | `dialog_states.active_request_id` |
| Intake progress | collecting/editing/ready + awaiting field | request/intake persistence |
| Regulation pending | отсутствует / typed clarification | `state_data.regulation_pending_clarification` |
| Intake conversation | split/category candidates/repeats | `state_data` через typed intake state |

Переходы:

- `/start` — **не переход**, только отображение greeting/active notice;
- `idle → text` — implicit new intake;
- `MENU_NEW` — mode=`intake`, regulation pending очищается; при отсутствии draft
  добавляется in-memory marker ожидания описания;
- `MENU_REGULATION` — mode=`regulation_qa`, active draft сохраняется;
- повторный `MENU_REGULATION` — mode остаётся тем же, pending **не очищается**;
- Regulation answer с missing slots — pending сохраняется;
- успешный answer, loop guard, выход в menu или переход в intake — pending
  очищается;
- completed request очищает active request state в persistence; confirm callback
  отдельно переводит Telegram mode в `intake` и предлагает новую заявку;
- restart adapter сохраняет persisted mode/pending, но in-memory marker новой
  заявки не сохраняет.

Опасная зона — отсутствие атомарного transition contract
`(mode, pending-kind, active-request) before → after`. CASE A и H показывают,
что menu/greeting semantics не совпадают с ожиданием пользователя о clean
context.

## 6. Category safety analysis

### Candidate generation

Приоритет источников:

1. persisted `category_candidates`;
2. exact/multiple deterministic classification draft;
3. derived classification из item/description/specifications/result/latest text;
4. generic fallback: services S01/S03/S05/S15, goods G01/G02/G03/G04.

### Confidence и provenance

У deterministic classifier нет score/confidence. Persisted option содержит
только code/label. Источник варианта и сила основания теряются. Поэтому
downstream не может отличить «classifier доказал G02» от «G02 оказался вторым
элементом общего fallback».

### Semantic compatibility

Validation проверяет:

- существование кода;
- соответствие G/S-префикса;
- mismatch с classifier exact/candidates, если classifier что-то нашёл;
- специальную допустимую альтернативу G03/G04 для монитора.

Она не проверяет positive support, когда classifier=`none`, и не использует
candidate provenance. Candidate membership также не является semantic proof.

### Readiness и confirm

Completeness видит заполненный syntactically valid category и может перейти в
ready. Lifecycle пересчитывает тот же контракт и не компенсирует отсутствующий
semantic proof. Поэтому безопасность должна быть восстановлена до readiness,
а confirm должен сохранять инвариант «unresolved/weak category не
регистрируется».

## 7. Domain/OOD analysis

| Класс | Текущее поведение | Проблема |
|---|---|---|
| Очевидный OOD: фильм/рецепт | нет known intent/procurement signal → refusal | работает |
| Procurement-looking personal | known brand/category/etc. intent → internal RAG | личная цель отсутствует в model/gate |
| Internal corporate | procurement intent + организационный контекст → RAG | работает, но organisational evidence не формализовано |

Нужен отдельный domain feature layer до retrieval, а не расширение claim regex.
Он должен учитывать и положительный internal context, и явный personal context.
При этом отсутствие слова «офис» не должно автоматически отклонять нормальный
вопрос о статусе/регламенте: canonical regulation intents сами могут быть
достаточным внутренним сигналом, тогда как товарно-брендовые consumer вопросы
требуют purpose distinction.

## 8. Clarification parsing analysis

В системе существуют три разных clarification contract:

1. Regulation pending — typed missing slots, merge и loop guard;
2. category/split selection — отдельные typed candidates и собственные parsers;
3. intake awaiting field — field code + обычный parser/validator.

Они не используют единый принцип evidence/provenance:

- Regulation хранит known/missing slots и fingerprint;
- category хранит candidates, но не source/confidence;
- split хранит rich candidates, но natural selection parser беднее category
  parser;
- awaiting free text хранит только expected field и принимает любую непустую
  строку.

Нельзя автоматически переназначать любой ответ в «похожее» поле: это создаст
новые silent errors. Безопасный контракт — detected semantic mismatch приводит
к уточнению/подтверждению, а не к угадыванию.

## 9. Test/evaluation coverage gap

| Case | Существующее покрытие | Почему прошло | Недостающий уровень |
|---|---|---|---|
| A | cancellation priority unit tests; Regulation navigation с active draft; pending loop tests | cancellation фразы короче; fake QA не классифицирует вопрос; re-entry same mode не проверен | real adapter + persistent dialog + cancellation paraphrase + повторный menu |
| B | obvious household OOD после завершённого context | тест — яблочный пирог, без procurement/brand vocabulary | contrast evaluation personal consumer vs office purchase |
| C | multicategory choices `компьютер`, `давайте компьютер`, `монтаж` | canonical token буквально присутствует | adapter conversation: long label, падеж, номер, command wrapper, ambiguous overlap |
| D | lifecycle mismatches: светильники/G01, стол/G01, ноутбук/G02 | classifier знает эти nouns и возвращает conflicting result | unknown noun + generic fallback + arbitrary same-prefix + confirm negative |
| E | unit aliases и CASE 1 invariant | пары не входят в lexicon cases | registry-level morphology matrix + adapter reload |
| F | awaiting parser normalization и system profile fields | assertions проверяют value type/field code, не semantic mismatch | adapter/persistence/card negative test для high-risk free text |
| G1 | production wiring service test с mocked provider | mock явно возвращает `procurement_type_raw=service` с evidence | hybrid merge test: deterministic service + structured null/rejected |
| G2 | service field ordering и card tests | сравниваются internal field codes, не тексты двух последовательных turns | adapter dialogue asserting distinct user questions and preserved fields |
| H | `/start` without draft; idle first text; mode persistence separately | не комбинируются persisted Regulation mode/pending + `/start` + full intake text | production wiring/persistent mode sequence |

Evaluation-наборы в основном single-turn или проверяют internal understanding.
Нужны stateful dialogue cases, где оцениваются фактический Telegram text,
persisted state после reload, readiness и запрет confirm.

## 10. Minimal stabilization scope перед следующим rehearsal

### P0 — блокирует защиту

1. **Category safety invariant**
   - убрать generic category fallback из набора selectable/confirmable options
     либо пометить его weak и не допускать readiness;
   - сохранять candidate provenance;
   - при classifier=`none` оставлять category unresolved;
   - добавить confirm-level invariant и unknown-subject negative tests.
2. **High-risk awaiting-field compatibility**
   - для department/contact/location и иных readiness-critical free-text полей
     блокировать очевидный semantic mismatch;
   - не переносить значение автоматически в другое поле;
   - проверять persistence/card/confirm.

### P1 — желательно до защиты

1. Clean transition при явном re-entry в Regulation Q&A; определить `/start`
   contract и тестировать mode/pending/active draft совместно.
2. Обобщить cancellation recognition без жёсткого 20-character window либо
   выделить action/target независимо.
3. Добавить personal-vs-internal purpose gate для consumer-looking intents.
4. Сохранить валидный deterministic `procurement_type` при structured
   null/rejection; structured explicit contradictory value должен идти в
   controlled conflict, а не silent overwrite.
5. Унифицировать безопасный selection contract split/category candidates.

### P2 — допустимые ограничения MVP

1. Расширить canonical unit lexicon подтверждёнными business units, включая
   пары; неизвестные units продолжать контролируемо отклонять.
2. Развести формулировки service `description` и `specifications`, не меняя
   обязательность и persistence schema.
3. Улучшить подсказку при неизвестной категории, не обещая автоматическую
   классификацию любого промышленного оборудования.

## 11. Что не следует исправлять сейчас

- Не добавлять hardcode только для холодильника, вентилятора, погрузчика или
  слова `пары` вне общего registry.
- Не подменять слабую классификацию первыми четырьмя категориями.
- Не разрешать любую same-prefix категорию ради завершения диалога.
- Не применять unrestricted fuzzy matching к codes, amounts, IDs и нормативным
  значениям.
- Не заставлять OpenAI выбирать окончательную категорию/тип без deterministic
  validation.
- Не переписывать retrieval, urgency, amount, lifecycle persistence, SQL,
  migrations или knowledge base: rehearsal не показал их непосредственной
  поломки.
- Не логировать raw user text, prompts, chunks или provider payload ради
  диагностики.

## 12. Рекомендуемый порядок исправлений

1. Закрыть P0 category invariant на candidate → state → readiness → confirm.
2. Закрыть P0 semantic mismatch для high-risk awaiting fields.
3. Зафиксировать state transition contract для `/start`, `MENU_REGULATION`,
   `MENU_NEW` и explicit re-entry; затем исправить A/H.
4. Защитить deterministic service type в hybrid merge и добавить G1 regression.
5. Ввести domain purpose distinction и contrast evaluation для CASE B.
6. Унифицировать natural selection для CASE C.
7. Выполнить P2: business units и разные service questions.
8. Только после полного локального regression и нового независимого smoke
   обновлять acceptance statements.

## 13. Existing tests/evaluations, которые нужно расширить

- `tests/test_regulation_question_understanding.py`:
  длинные cancellation paraphrases; personal/internal contrast pairs.
- `tests/test_regulation_conversation.py`:
  pending → повторный `MENU_REGULATIONS`; cancellation turn без retrieval
  clarification; `/start`/mode sequence через adapter-level harness.
- `tests/test_telegram_navigation.py`:
  completed и active intake + Regulation re-entry + clean pending semantics.
- `tests/test_telegram_multicategory.py`:
  number, partial noun, падеж, длинный candidate, ambiguous overlap; вентилятор
  с отсутствующей классификацией.
- `tests/test_request_lifecycle.py`:
  unknown subject + arbitrary same-prefix category не confirmable.
- `tests/test_telegram_adapter_ux.py`:
  `пара/пары/пар`; semantic mismatch awaiting department; различимые service
  description/specifications questions.
- `tests/test_telegram_hybrid_extraction.py`:
  deterministic service + structured null, rejected evidence и contradictory
  type.
- `tests/test_telegram_production_wiring.py`:
  persistent mode/pending across adapter reload; full CASE C–G dialogue through
  actual wiring without external calls.
- conversation/evaluation datasets:
  добавить новые последовательности как holdout-class cases, оценивать
  фактический answer, state transition, source/reason и confirmability, а не
  только internal object.

## 14. Observability improvements для следующего rehearsal

### Что есть сейчас

Regulation log безопасно содержит mode, retrieval status, counts chunks/sources,
duration, reason/error code и hashed message reference. Extraction debug при
явном флаге содержит field names, небольшие scalar values, rejected fields и
completed/missing/next field. Полные user texts, prompts и chunks не нужны и не
должны добавляться.

### Чего не хватает

- `current_intent before/after` и transition trigger;
- был ли pending и какого он типа до/после transition;
- primary/secondary intent и domain decision как enum;
- missing slots и clarification decision;
- category candidate source (`classifier_exact`, `classifier_multiple`,
  `generic_fallback`, `persisted`) и eligibility;
- decomposition selection method (`number`, `exact_label`, `token_subset`,
  `type_alias`, `no_match`);
- merge decision для `procurement_type` и rejection reason;
- semantic guard decision (`supported`, `mismatch`, `unresolved`);
- безопасный state version до/после для выявления race.

### Рекомендуемые safe decision IDs

- `dialog.transition.regulation_enter_clean` /
  `dialog.transition.regulation_reenter_pending`;
- `dialog.route.regulation_pending`, `dialog.route.regulation_new`,
  `dialog.route.intake`, `dialog.route.idle_implicit_intake`;
- `rag.intent.request_cancellation`, `rag.intent.ambiguous_followup`;
- `rag.domain.internal`, `rag.domain.personal`, `rag.domain.ambiguous`;
- `rag.clarification.created`, `resolved`, `repeated`, `step_limit`;
- `intake.merge.type_deterministic`, `structured`, `conflict`, `missing`;
- `split.selection.number`, `exact`, `token`, `ambiguous`, `no_match`;
- `category.candidates.classifier`, `derived`, `fallback`, `persisted`;
- `category.guard.supported`, `mismatch`, `unresolved`;
- `awaiting.compatible`, `mismatch`, `needs_confirmation`.

Логировать следует только enum/decision ID, field names, counts, state version и
hashed correlation ID — без текста, PII, chunks, prompt и provider response.

## 15. Acceptance statements, которые снова нельзя считать подтверждёнными

До исправления и нового независимого production rehearsal нельзя утверждать:

- что явный вход в Regulation Q&A всегда начинает clean context;
- что `/start` предсказуемо восстанавливает или сбрасывает режим;
- что все естественные вопросы об отмене распознаются как cancellation;
- что Regulation Q&A отличает внутреннюю закупку от явно личной покупки;
- что любой показанный category candidate предметно релевантен;
- что semantic category guard блокирует arbitrary same-prefix category;
- что confirmable карточка всегда содержит семантически корректные department
  и category;
- что natural choice любой decomposed need работает по номеру/частичной форме;
- что hybrid extraction всегда сохраняет очевидный deterministic service type;
- что два последовательных service questions однозначно объясняют разные поля;
- что canonical unit lexicon покрывает типовые закупочные единицы.

При этом остаются подтверждёнными более узкие statements из раздела 2:
исправленные amount forms, обнаружение конкретного mixed-сценария, очевидный
OOD refusal, urgency answer и отсутствие видимых инфраструктурных сбоев в
предоставленной выдержке.

## Follow-up: category generalization после P0-D

Production smoke после P0-D подтвердил отдельный coverage gap: «офисные стулья»
не входили в deterministic G02 vocabulary. До P0-D это маскировал общий fallback
G01–G04; после его правильного удаления категория безопасно оставалась unresolved.

Follow-up реализован без возврата fallback и без расширения regex-каталога каждым
новым предметом. После deterministic `none` применяется strict closed-taxonomy
provider на общем OpenAI client. Его exact/candidate результат не попадает в draft
до явного подтверждения. Candidate state хранит provenance, тип и subject
fingerprint; подтверждённое поле получает `llm_confirmed` с fingerprint и кодом.
Readiness и lifecycle отклоняют unconfirmed/weak/stale provenance. Ошибка provider
сохраняет unresolved category и controlled clarification.

Добавлен отдельный offline generalization dataset: офисное оснащение, IT
accessory, промышленное оборудование, хозяйственный товар, услуга, ambiguous и
truly unresolved. Knowledge base, embeddings, SQL и migrations для follow-up не
изменялись. Статус production acceptance этим локальным изменением не повышается.

## Follow-up после real category-provider smoke

Первый real smoke выявил два ограниченных дефекта: literal evidence отклонял
безопасное русское словоизменение (`настенная`/`настенную`), а metadata недостаточно
чётко разделяла S01 и S15. Evidence matching переведён на консервативную
нормализацию с непрерывным token/stem match; совпадение только общих слов остаётся
недостаточным. Taxonomy metadata повышена с `intake-categories-v1` до
`intake-categories-v2`: S01 описывает ремонт/монтаж/ТО и восстановление
работоспособности, S15 — не являющиеся ими метрологические, поверочные,
калибровочные, испытательные и экспертные услуги. Noun hardcodes не добавлялись.
