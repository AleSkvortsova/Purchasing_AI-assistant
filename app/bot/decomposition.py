import re
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from app.bot.categories import DeterministicCategoryClassifier

DecompositionKind = Literal[
    "single_need",
    "multiple_goods",
    "multiple_services",
    "goods_plus_service",
    "ambiguous",
]

_GOODS_ACTION = re.compile(
    r"\b(?:купить|закупить|приобрести|поставить)\w*\s+",
    re.IGNORECASE,
)
_SERVICE_START = re.compile(
    r"\s+и\s+(?=(?:выполнить\s+(?:(?:их|его|её)\s+)?"
    r"(?:монтаж|укладку)|установить\w*|смонтировать\w*|"
    r"собрать\w*|заправить\w*))",
    re.IGNORECASE,
)
_LEADING_QUANTITY = re.compile(
    r"^(?P<quantity>\d+(?:[.,]\d+)?)\s+",
    re.IGNORECASE,
)
_COMMON_CONTEXT = re.compile(
    r"\b(?:до|к)\s+\d{1,2}\s+[а-яё]+|"
    r"\b(?:по адресу|на площадке|в офисе|на складе)\b.*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProcurementNeed:
    procurement_type: Literal["goods", "service"]
    subject: str
    action: str
    evidence: str
    category_code: str | None = None
    category_candidates: tuple[str, ...] = ()
    quantity: Decimal | None = None
    relation: str = "separate_request"


@dataclass(frozen=True)
class ProcurementDecomposition:
    kind: DecompositionKind
    needs: tuple[ProcurementNeed, ...] = ()
    common_context: str | None = None
    fingerprint: str = ""


def decompose_procurement_needs(text: str) -> ProcurementDecomposition:
    """Detect an explicit acquisition plus a separately requested service."""
    goods_action = _GOODS_ACTION.search(text)
    if goods_action is None:
        return _result("single_need", text)
    service_start = _SERVICE_START.search(text, goods_action.end())
    if service_start is None:
        return _result("single_need", text)

    goods_evidence = text[goods_action.start() : service_start.start()].strip(" ,.")
    service_evidence = text[service_start.end() :].strip(" ,.")
    goods_subject_raw = text[goods_action.end() : service_start.start()].strip(" ,.")
    goods_subject_raw = re.split(
        r"\s+(?:для|к|до)\s+",
        goods_subject_raw,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    quantity_match = _LEADING_QUANTITY.match(goods_subject_raw)
    quantity = None
    if quantity_match is not None:
        quantity = Decimal(quantity_match.group("quantity").replace(",", "."))
        goods_subject_raw = goods_subject_raw[quantity_match.end() :]
    goods_subject = goods_subject_raw.strip(" ,.")
    if not goods_subject:
        return _result("ambiguous", text)

    service_action = _service_action(service_evidence)
    service_subject = _service_subject(service_evidence, goods_subject)
    goods_category, goods_candidates = _category(goods_subject, "goods")
    service_category, service_candidates = _category(
        f"{service_action} {service_subject}", "service"
    )
    if service_category is None:
        service_category = _service_category(service_action)

    needs = (
        ProcurementNeed(
            procurement_type="goods",
            subject=goods_subject,
            action=goods_action.group().strip(),
            evidence=goods_evidence,
            category_code=goods_category,
            category_candidates=goods_candidates,
            quantity=quantity,
        ),
        ProcurementNeed(
            procurement_type="service",
            subject=service_subject,
            action=service_action,
            evidence=service_evidence,
            category_code=service_category,
            category_candidates=service_candidates,
        ),
    )
    context_matches = list(_COMMON_CONTEXT.finditer(text))
    context_match = next(
        (
            match
            for match in reversed(context_matches)
            if any(
                marker in match.group().casefold()
                for marker in ("адрес", "площадк", "офис", "склад")
            )
        ),
        context_matches[-1] if context_matches else None,
    )
    return _result(
        "goods_plus_service",
        text,
        needs,
        context_match.group().strip() if context_match else None,
    )


def _result(
    kind: DecompositionKind,
    text: str,
    needs: tuple[ProcurementNeed, ...] = (),
    common_context: str | None = None,
) -> ProcurementDecomposition:
    fingerprint = sha256(
        " ".join(text.casefold().replace("ё", "е").split()).encode("utf-8")
    ).hexdigest()[:16]
    return ProcurementDecomposition(kind, needs, common_context, fingerprint)


def _category(
    text: str,
    procurement_type: Literal["goods", "service"],
) -> tuple[str | None, tuple[str, ...]]:
    classification = DeterministicCategoryClassifier().classify(
        text, procurement_type
    )
    if classification.kind == "exact":
        return classification.category_code, ()
    return None, classification.candidates


def _service_action(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    for marker, label in (
        ("монтаж", "монтаж"),
        ("уклад", "укладка"),
        ("установ", "установка"),
        ("смонтир", "монтаж"),
        ("собра", "сборка"),
        ("заправ", "заправка"),
    ):
        if marker in normalized:
            return label
    return "выполнение работ"


def _service_subject(service_text: str, goods_subject: str) -> str:
    normalized = service_text.casefold().replace("ё", "е")
    if any(pronoun in normalized for pronoun in (" его ", " их ", " её ")):
        return f"{_service_action(service_text)} {goods_subject}"
    remainder = re.sub(
        r"^(?:выполнить\s+)?(?:монтаж|укладку|установить\w*|"
        r"смонтировать\w*|собрать\w*|заправить\w*)\s*",
        "",
        service_text,
        flags=re.IGNORECASE,
    )
    remainder = re.split(
        r"\s+(?:до|к|на площадке|по адресу)\s+",
        remainder,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.")
    target = remainder or goods_subject
    return f"{_service_action(service_text)} {target}".strip()


def _service_category(action: str) -> str | None:
    if action in {"монтаж", "укладка", "установка"}:
        return "S01"
    if action == "сборка":
        return "S15"
    return None
