import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.rag.value_normalization import (
    BudgetStatus,
    detect_budget_status,
    normalize_money_amount,
    normalize_regulation_text,
    parse_duration_days,
)

RegulationQuestionIntent = Literal[
    "approval_route",
    "urgency_policy",
    "status_explanation",
    "request_cancellation",
    "required_fields",
    "category_classification",
    "brand_equivalent_policy",
    "responsibility_policy",
    "draft_and_history",
    "supplier_recommendation",
    "general_help",
    "ambiguous_followup",
    "outside_domain",
]
RegulationDomainDecision = Literal[
    "known_domain_intent",
    "ambiguous_domain",
    "outside_domain",
]

_EVENT_TERMS = re.compile(
    r"\b(?:мероприят\w*|конференц\w*|форум\w*|семинар\w*|выставк\w*|"
    r"презентац\w*|корпоративн\w*\s+событи\w*|"
    r"организац\w*\s+площадк\w*|делов\w*\s+встреч\w*|"
    r"встреч\w*.*внешн\w*\s+организац\w*)\b"
)
_NON_PROCUREMENT_MEETING = re.compile(
    r"\b(?:созвон\w*|внутренн\w*\s+совещан\w*|обычн\w*\s+встреч\w*)\b"
)


class RegulationQuestionUnderstanding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_intent: RegulationQuestionIntent
    secondary_intents: tuple[RegulationQuestionIntent, ...] = ()
    amount: Decimal | None = None
    budget_status: BudgetStatus | None = None
    duration_days: int | None = None
    relative_deadline: Literal[
        "today",
        "tomorrow",
        "day_after_tomorrow",
        "in_days",
        "in_weeks",
        "in_month",
    ] | None = None
    status_name: str | None = None
    purchase_subject: str | None = None
    purchase_type: Literal["goods", "service"] | None = None
    category_hint: str | None = None
    known_fields: tuple[str, ...] = ()
    missing_required_context: tuple[str, ...] = ()
    requires_clarification: bool = False
    clarifying_question: str | None = None
    outside_kb_intent: bool = False
    domain_decision: RegulationDomainDecision
    normalized_question: str

    @property
    def intents(self) -> tuple[RegulationQuestionIntent, ...]:
        return (self.primary_intent, *self.secondary_intents)


def understand_regulation_question(question: str) -> RegulationQuestionUnderstanding:
    normalized = normalize_intent_typos(normalize_regulation_text(question))
    amount = normalize_money_amount(question)
    budget_status = detect_budget_status(question)
    duration_days, relative_deadline = _deadline(question)
    purchase_type = _purchase_type(normalized)
    category_hint = _category_hint(normalized)
    purchase_subject = _purchase_subject(normalized)
    status_name = _status_name(normalized)
    intents = _intents(normalized, status_name, category_hint)
    primary = intents[0]
    domain_decision = _domain_decision(primary, normalized)
    missing, clarification = _clarification(
        primary,
        normalized=normalized,
        amount=amount,
        budget_status=budget_status,
        purchase_subject=purchase_subject,
        purchase_type=purchase_type,
        category_hint=category_hint,
    )
    if _has_personal_purpose(normalized) and _has_organisational_purpose(
        normalized
    ):
        missing = ("purchase_purpose",)
        clarification = (
            "Уточните, покупка нужна для личного использования или для "
            "рабочих задач организации?"
        )
    known_fields = tuple(
        name
        for name, value in (
            ("amount", amount),
            ("budget_status", budget_status),
            ("duration_days", duration_days),
            ("status_name", status_name),
            ("purchase_subject", purchase_subject),
            ("purchase_type", purchase_type),
            ("category_hint", category_hint),
        )
        if value is not None
    )
    return RegulationQuestionUnderstanding(
        primary_intent=primary,
        secondary_intents=tuple(intents[1:]),
        amount=amount,
        budget_status=budget_status,
        duration_days=duration_days,
        relative_deadline=relative_deadline,
        status_name=status_name,
        purchase_subject=purchase_subject,
        purchase_type=purchase_type,
        category_hint=category_hint,
        known_fields=known_fields,
        missing_required_context=missing,
        requires_clarification=clarification is not None,
        clarifying_question=clarification,
        outside_kb_intent=primary == "supplier_recommendation",
        domain_decision=domain_decision,
        normalized_question=normalized,
    )


def _intents(
    value: str,
    status_name: str | None,
    category_hint: str | None,
) -> tuple[RegulationQuestionIntent, ...]:
    if _supplier_recommendation(value):
        return ("supplier_recommendation",)
    if _general_help(value):
        return ("general_help",)
    if _ambiguous_followup(value):
        return ("ambiguous_followup",)

    intents: list[RegulationQuestionIntent] = []
    asks_cancellation = _asks_for_cancellation(value)
    draft_action = is_draft_question(value)
    explicit_status = _asks_about_status(value)
    asks_status = (status_name is not None or explicit_status) and not (
        draft_action and not explicit_status
    )
    if asks_cancellation:
        intents.append("request_cancellation")
    if asks_status and not asks_cancellation:
        intents.append("status_explanation")
    if _asks_for_approval(value) and not asks_status and not asks_cancellation:
        intents.append("approval_route")
    if not draft_action and _procurement_domain_signal(value) and re.search(
        r"\b(?:сроч\w*|приоритет\w*|за\s+сколько\s+дн|"
        r"сегодня|завтра|послезавтра|через\s+\d+\s+(?:д|недел|месяц)|"
        r"остал\w*.{0,12}\d+\s+(?:д|недел))",
        value,
    ):
        intents.append("urgency_policy")
    if draft_action or is_history_question(value) or re.search(
        r"\bустн\w*", value
    ):
        intents.append("draft_and_history")
    if re.search(r"\b(?:бренд\w*|марк\w*|эквивалент\w*)", value):
        intents.append("brand_equivalent_policy")
    if re.search(
        r"\bкто\s+(?:обязан|отвечает|должен\s+подготов)|ответствен\w*",
        value,
    ):
        intents.append("responsibility_policy")
    asks_submission = bool(
        re.search(r"\bможно\s+ли\s+подать\w*.{0,20}заяв\w*", value)
    )
    asks_fields = _asks_for_fields(value) or asks_submission
    asks_category = bool(re.search(r"\bкатегор\w*|к\s+какой\s+категор", value))
    if asks_category and _procurement_domain_signal(value):
        intents.append("category_classification")
    elif category_hint is not None and (
        asks_fields
        and not asks_submission
        and category_hint in {"S03", "S05"}
    ):
        intents.append("category_classification")
    if asks_fields:
        intents.append("required_fields")
    if re.search(
        r"\b(?:одн\w*\s+заяв|\d+\s+заяв|вместе|объедин\w*)\b",
        value,
    ) and (
        (
            re.search(r"компьютер|товар|оборудован", value)
            and re.search(r"установ|программ|услуг", value)
        )
        or (
            re.search(r"товар|кресл|оборудован", value)
            and re.search(r"лиценз|услуг|достав", value)
        )
    ):
        intents.extend(("category_classification", "required_fields"))
    if intents:
        return tuple(dict.fromkeys(intents))
    if _procurement_domain_signal(value):
        return ("ambiguous_followup",)
    return ("outside_domain",)


def _domain_decision(
    intent: RegulationQuestionIntent,
    value: str,
) -> RegulationDomainDecision:
    personal = _has_personal_purpose(value)
    organisational = _has_organisational_purpose(value)
    if personal and organisational:
        return "ambiguous_domain"
    if personal:
        return "outside_domain"
    if intent == "outside_domain":
        return "outside_domain"
    if intent == "ambiguous_followup":
        return "ambiguous_domain"
    return "known_domain_intent"


def _has_personal_purpose(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:себе|домой|для\s+дома|лично|для\s+семьи|"
            r"в\s+квартиру|для\s+дачи|для\s+личного\s+использования)\b",
            value,
        )
    )


def _has_organisational_purpose(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:в\s+офис(?:ную|е)?|для\s+офиса|для\s+подразделения|"
            r"сотрудник(?:у|ам|ов)?|на\s+склад|для\s+производства|"
            r"для\s+компании|для\s+рабочего\s+места|"
            r"для\s+мероприятия\s+компании|для\s+бухгалтерии)\b",
            value,
        )
    )


def _procurement_domain_signal(value: str) -> bool:
    """Require a positive procurement signal before regulation retrieval."""
    return bool(
        re.search(
            r"\b(?:закуп\w*|заяв\w*|черновик\w*|товар\w*|услуг\w*|"
            r"поставк\w*|поставщик\w*|подрядчик\w*|перевоз\w*|груз\w*|"
            r"бюджет\w*|внебюджет\w*|соглас\w*|одобр\w*|категор\w*|"
            r"сроч\w*|бренд\w*|эквивалент\w*|отмен\w*|закупщик\w*|"
            r"оборудован\w*|мебел\w*|лиценз\w*)\b",
            value,
        )
        or is_procurement_event(value)
    )


def _asks_for_approval(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:кто\w*.{0,20}соглас\w*|"
            r"через\s+кого\w*.{0,20}(?:проход\w*|пройт\w*)|"
            r"маршрут\w*(?:\s+согласован\w*)?|"
            r"чье\s+(?:согласован\w*\s+нужн\w*|одобрен\w*)|"
            r"кто\w*\s+должен\s+одобр\w*|"
            r"кому\w*.*согласован\w*|какие\w*\s+согласован\w*|"
            r"кто\w*.{0,20}согласующ\w*|согласующ\w*)\b",
            value,
        )
    )


def _asks_about_status(value: str) -> bool:
    return bool(
        re.search(
            r"(?:како\w*\s+(?:сейчас\s+|текущ\w*\s+)?статус|"
            r"что\s+означа\w*.*статус|какие\w*.*статус|"
            r"почему\w*.*статус|что\s+делать.*статус|"
            r"отправил\w*\s+заяв\w*\s+в\s+закуп|"
            r"закупщик\w*\s+вернул\w*\s+заяв)",
            value,
        )
    )


def _asks_for_cancellation(value: str) -> bool:
    action = bool(
        re.search(
            r"\b(?:отмен\w*|снят\w*|снять|отказ\w*|удал\w*|"
            r"убра\w*|не\s+отправля\w*|останов\w*)\b",
            value,
        )
        or re.search(
            r"\bпотребност\w*\s+(?:исчезл\w*|отменен\w*|отпал\w*)",
            value,
        )
        or re.search(r"\bбольше\s+не\s+нужн\w*", value)
    )
    target = bool(re.search(r"\b(?:заяв\w*|черновик\w*|запрос\w*|закуп\w*)\b", value))
    return action and target


def _asks_for_fields(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:что|какие|что\s+именно)\b.{0,35}"
            r"(?:указ|напис|писат|заполн|нуж|параметр|пол)|"
            r"\b(?:сведен|данн|параметр|пол)\w*.{0,20}"
            r"(?:нуж|обязательн|указ|заполн)|\bкак\s+оформ\w*.{0,20}"
            r"(?:заяв|закуп|перевоз|услуг|товар|потреб)",
            value,
        )
    )


def is_draft_question(value: str) -> bool:
    return bool(
        re.search(
            r"\b(?:черновик\w*|сохран\w*.{0,30}(?:незаполн\w*|заяв\w*)|"
            r"остав\w*.{0,20}незакончен\w*|"
            r"вернут\w*.{0,25}(?:заполн\w*|поздн\w*|потом)|"
            r"продолж\w*.{0,15}(?:потом|поздн|позже)|"
            r"продолж\w*.{0,35}(?:ранее|начат\w*|заяв\w*)|"
            r"(?:не\s+)?законч\w*.{0,35}(?:потом|поздн|завтра)|"
            r"не\s+законч\w*.{0,60}продолж\w*|"
            r"нача\w*.{0,25}заяв\w*.{0,35}законч\w*.{0,15}потом|"
            r"пока\s+не\s+зна\w*\s+все\s+данн\w*|"
            r"заполн\w*.{0,10}(?:частичн\w*|часть)|"
            r"не\s+.{0,15}отправля\w*.{0,15}сейчас|"
            r"незакончен\w*)\b",
            value,
        )
    )


def is_history_question(value: str) -> bool:
    if re.search(r"\b(?:текущ\w*|активн\w*)\s+заяв\w*", value):
        return False
    return bool(
        re.search(
            r"\b(?:мои\s+(?:предыдущ\w*\s+|стар\w*\s+)?заяв\w*|"
            r"истори\w*\s+моих\s+заяв\w*|"
            r"(?:предыдущ\w*|стар\w*|ранее\s+создан\w*)\s+заяв\w*|"
            r"последн\w*.{0,20}(?:отправлен\w*\s+)?заяв\w*|"
            r"недавн\w*\s+заяв\w*|что\s+я\s+(?:подавал\w*|отправлял\w*)|"
            r"что\s+я\s+уже\s+(?:подавал\w*|отправлял\w*)|"
            r"что\s+я\s+(?:подавал\w*|отправлял\w*)\s+до\s+этого|"
            r"заяв\w*.{0,25}(?:я\s+)?подавал\w*\s+раньше|"
            r"прошл\w*\s+обращен\w*|"
            r"как\w*\s+заяв\w*.{0,20}недавн\w*\s+отправ\w*|"
            r"где.{0,20}(?:прошл\w*|раньше|ранее|создан\w*|отправлен\w*)"
            r".{0,15}заяв\w*|посмотр\w*.{0,20}(?:ранее|прошл\w*|создан\w*)"
            r".{0,15}заяв\w*)\b",
            value,
        )
    )


def _clarification(
    intent: RegulationQuestionIntent,
    *,
    normalized: str,
    amount: Decimal | None,
    budget_status: BudgetStatus | None,
    purchase_subject: str | None,
    purchase_type: str | None,
    category_hint: str | None,
) -> tuple[tuple[str, ...], str | None]:
    if intent == "approval_route":
        missing = []
        if amount is None:
            missing.append("amount")
        if budget_status not in {"budgeted", "unbudgeted"}:
            missing.append("budget_status")
        if missing == ["budget_status"]:
            return (
                tuple(missing),
                "Уточните, пожалуйста, предусмотрена ли закупка бюджетом. "
                "От этого зависит маршрут согласования.",
            )
        if missing:
            return (
                tuple(missing),
                "Уточните сумму закупки и предусмотрена ли она бюджетом.",
            )
    if intent == "ambiguous_followup" and purchase_subject is None:
        if re.search(r"соглас", normalized):
            return (
                ("amount", "budget_status"),
                "Уточните сумму закупки и предусмотрена ли она бюджетом.",
            )
        if re.search(r"что\s+мне\s+делать\s+с\s+(?:этой|моей)\s+заяв", normalized):
            return (
                ("status_name",),
                "Уточните текущий статус заявки или опишите, что с ней произошло.",
            )
        return (
            ("purchase_subject", "purchase_type"),
            "Уточните, что вы хотите закупить: товар или услугу, и кратко "
            "опишите предмет закупки.",
        )
    if (
        intent == "required_fields"
        and purchase_subject is None
        and purchase_type is None
        and category_hint is None
        and re.search(
            r"(?:что.*(?:указ|заполн)|какие.*(?:данн|сведен|пол)|"
            r"обязательн\w*.*(?:данн|сведен|пол))",
            normalized,
        )
    ):
        return (
            ("purchase_subject", "purchase_type"),
            "Уточните, что вы хотите закупить: товар или услугу, и кратко "
            "опишите предмет закупки.",
        )
    return (), None


def _supplier_recommendation(value: str) -> bool:
    supplier_subject = bool(
        re.search(
            r"поставщик|перевозчик|подрядчик|поставляет|"
            r"транспортн\w*\s+компан",
            value,
        )
    )
    return supplier_subject and bool(
        re.search(
            r"\b(?:кто|какой|какого|кого)\b.*\b(?:лучш\w*|дешев\w*|"
            r"надежн\w*|выбрать|посовет\w*|рекоменд\w*|предпоч\w*|услов\w*)|"
            r"\b(?:посовет\w*|рекоменд\w*)\b.*"
            r"\b(?:поставщик|перевозчик|подрядчик|"
            r"транспортн\w*\s+компан)|"
            r"\b(?:цен\w*|стоимост\w*|тариф\w*|рыночн\w*)\b",
            value,
        )
    )


def _general_help(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:(?:расскажи|объясни)\s+(?:все|вкратце)\s+про\s+закупк\w*|"
            r"(?:дай|покажи)\s+(?:краткий\s+)?обзор\s+(?:правил\s+)?закупок|"
            r"можно\s+(?:коротко|вкратце)\s+(?:обо\s+всех|про)\s+"
            r"(?:правил\w*\s+)?закупок)\??\.?",
            value,
        )
    )


def _ambiguous_followup(value: str) -> bool:
    return bool(
        re.fullmatch(r"кто\s+(?:это\s+)?должен\s+согласовать\??", value)
        or re.fullmatch(r"что\s+(?:мне\s+)?указать\??", value)
        or re.fullmatch(r"что\s+мне\s+делать\s+с\s+этой\s+заявк\w*\??", value)
        or re.fullmatch(
            r"(?:да|нет|не\s+знаю|предусмотрен\w*|товар|услуг\w*)[.!?]?",
            value,
        )
    )


def _deadline(value: str) -> tuple[int | None, str | None]:
    basic = value.casefold().replace("ё", "е")
    if re.search(r"\bпослезавтра\b", basic):
        return 2, "day_after_tomorrow"
    if re.search(r"\bзавтра\b", basic):
        return 1, "tomorrow"
    if re.search(r"\bсегодня\b", basic):
        return 0, "today"
    if re.search(r"\bчерез\s+месяц\b", basic):
        return 30, "in_month"
    normalized = normalize_regulation_text(value)
    duration = parse_duration_days(normalized)
    if duration is None:
        return None, None
    kind = "in_weeks" if "недел" in basic else "in_days"
    return duration, kind


def _purchase_type(value: str) -> Literal["goods", "service"] | None:
    goods = bool(re.search(r"товар|оборудован|компьютер|техник|поставка", value))
    service = bool(
        re.search(r"услуг|работ|разработ|интеграц|подключ|установ", value)
        or is_procurement_event(value)
    )
    if goods and not service:
        return "goods"
    if service and not goods:
        return "service"
    return None


def _category_hint(value: str) -> str | None:
    if re.search(r"перевез|перевоз|транспорт|груз|паллет|короб", value):
        return "S03"
    if re.search(r"интеграц|подключ|обмен|1с|crm|сайт", value):
        return "S05"
    if is_procurement_event(value):
        return "S07"
    return None


def _purchase_subject(value: str) -> str | None:
    if is_procurement_event(value):
        return "мероприятие"
    marker = re.search(
        r"\b(оборудован\w*|компьютер\w*|техник\w*|товар\w*|услуг\w*|"
        r"мероприят\w*|перевоз\w*|груз\w*|интеграц\w*|crm|1с|сайт)\b",
        value,
    )
    if marker is None:
        return None
    subject = marker.group(1)
    canonical = {
        "оборудован": "оборудование",
        "компьютер": "компьютеры",
        "техник": "техника",
        "товар": "товар",
        "услуг": "услуга",
        "мероприят": "мероприятие",
    }
    return next(
        (name for stem, name in canonical.items() if subject.startswith(stem)),
        subject,
    )


def is_procurement_event(value: str) -> bool:
    return bool(
        _EVENT_TERMS.search(value) and not _NON_PROCUREMENT_MEETING.search(value)
    )


def _status_name(value: str) -> str | None:
    if re.search(r"закупщик\w*\s+вернул\w*\s+заяв|требует\s+доработки", value):
        return "requires_rework"
    if re.search(r"отправил\w*\s+заяв\w*\s+в\s+закуп|передана\s+в\s+отдел", value):
        return "transferred_to_procurement"
    if re.search(r"заяв\w*.{0,20}(?:ушл\w*|направлен\w*)\s+на\s+согласован", value):
        return "на согласовании"
    if re.search(
        r"заяв\w*.{0,20}(?:у\s+согласующ\w*|рассматрива\w*\s+согласующ)",
        value,
    ):
        return "на согласовании"
    phrases = (
        "принята в работу",
        "на согласовании",
        "поставка ожидается",
        "в работе",
        "выполнена",
        "отклонена",
        "черновик",
    )
    return next((item for item in phrases if item in value), None)


_INTENT_TYPO_TARGETS = (
    "посоветуйте",
    "поставщик",
    "перевозчик",
    "согласование",
    "отменить",
    "заявка",
    "бюджет",
)
_INTENT_VALID_STEMS = (
    "посовет",
    "поставщ",
    "перевозч",
    "согласован",
    "отмен",
    "заяв",
    "бюджет",
)


def normalize_intent_typos(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if any(token.startswith(stem) for stem in _INTENT_VALID_STEMS):
            return token
        closest = next(
            (
                target
                for target in _INTENT_TYPO_TARGETS
                if abs(len(token) - len(target)) <= 1
                and _edit_distance_at_most_one(token, target)
            ),
            None,
        )
        return closest or token

    return re.sub(r"[а-я]+", replace, value)


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) <= 1
    if len(left) > len(right):
        left, right = right, left
    index_left = 0
    index_right = 0
    edits = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
        else:
            edits += 1
            if edits > 1:
                return False
        index_right += 1
    return True
