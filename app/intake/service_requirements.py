import re

from app.intake.models import RequestDraftData, UpdateSource

_LEADING_RESULT_VERB = re.compile(
    r"^(?:должн\w*\s+быть\s+)?(?:проведен\w*|выполнен\w*|"
    r"оказан\w*|организован\w*)\s+",
    re.IGNORECASE,
)
_WORKDAY_PHRASE = re.compile(
    r"\bв\s+рамках\s+рабочего\s+дня\b",
    re.IGNORECASE,
)
_VOLUME = re.compile(
    r"^(?:\d+(?:[.,]\d+)?|один|одна|одно|два|две|три|четыре|"
    r"пять|шесть|семь|восемь|девять|десять|одиннадцать|"
    r"двенадцать|тринадцать|четырнадцать|пятнадцать|"
    r"шестнадцать|семнадцать|восемнадцать|девятнадцать|"
    r"двадцать(?:\s+\w+)?)\s+[а-яё][а-яё-]*(?:\s+[а-яё][а-яё-]*)?$",
    re.IGNORECASE,
)


def normalize_service_requirements(draft: RequestDraftData) -> str | None:
    """Build deterministic, presentation-ready service requirements."""
    candidates = _ordered_candidates(draft)
    result: list[str] = []
    for value in candidates:
        for raw_segment in re.split(r"[.;]+|,\s+", value):
            segment = _clean_segment(raw_segment, draft.item_name)
            if not segment or _duplicates_desired_result(segment, draft):
                continue
            if any(_same_meaning(segment, existing) for existing in result):
                continue
            result.append(segment)
    volumes = [segment for segment in result if _VOLUME.fullmatch(segment)]
    content = [segment for segment in result if segment not in volumes]
    if volumes and content:
        content[0] = f"{content[0]} ({'; '.join(volumes)})"
    elif volumes:
        content.extend(volumes)
    if not content:
        return None
    return "; ".join(_sentence(segment) for segment in content) + "."


def normalize_service_desired_result(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.strip(" ,.;").split())
    if _LEADING_RESULT_VERB.match(cleaned) and len(cleaned.split()) <= 2:
        return None
    return cleaned


def _ordered_candidates(draft: RequestDraftData) -> list[str]:
    candidates: list[tuple[bool, int, str]] = []
    for order, code in enumerate(("description", "specifications")):
        value = getattr(draft, code)
        if not value:
            continue
        state = draft.field_states.get(code)
        confirmed = bool(
            state and state.source == UpdateSource.USER and state.confirmed
        )
        candidates.append((not confirmed, order, value))
    return [value for _, _, value in sorted(candidates)]


def _clean_segment(value: str, item_name: str | None) -> str:
    cleaned = " ".join(value.strip(" ,.;:—–-").split())
    cleaned = _LEADING_RESULT_VERB.sub("", cleaned)
    cleaned = _WORKDAY_PHRASE.sub("в течение рабочего дня", cleaned)
    if item_name:
        without_item = re.sub(
            re.escape(item_name),
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        ).strip(" ,.;:—–-")
        if _is_standalone_requirement(without_item):
            cleaned = without_item
    return cleaned.strip(" ,.;:—–-")


def _is_standalone_requirement(value: str) -> bool:
    if not value:
        return False
    words = value.casefold().split()
    return len(words) >= 3 or words[0] in {
        "в",
        "для",
        "до",
        "на",
        "по",
        "после",
        "при",
        "с",
    }


def _duplicates_desired_result(segment: str, draft: RequestDraftData) -> bool:
    desired = normalize_service_desired_result(draft.desired_result)
    return bool(desired and _same_meaning(segment, desired))


def _same_meaning(left: str, right: str) -> bool:
    left_normalized = _normalize(left)
    right_normalized = _normalize(right)
    return bool(
        left_normalized
        and right_normalized
        and (
            left_normalized == right_normalized
            or left_normalized in right_normalized
            or right_normalized in left_normalized
        )
    )


def _normalize(value: str) -> str:
    return " ".join(
        re.findall(r"[a-zа-я0-9]+", value.casefold().replace("ё", "е"))
    )


def _sentence(value: str) -> str:
    return value[:1].upper() + value[1:]
