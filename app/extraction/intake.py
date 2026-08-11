import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Literal

from app.bot.normalization import (
    NaturalDateParser,
    amount_evidence,
    normalize_unit,
    parse_amount_expression,
    parse_cardinal,
)
from app.extraction.exceptions import ApprovalExtractionProviderError
from app.extraction.models import RawApprovalExtraction
from app.extraction.normalization import evidence_supports_field
from app.extraction.provider import ApprovalExtractionProvider
from app.intake.field_registry import CATEGORY_NAMES
from app.intake.models import (
    IntakeFieldUpdate,
    NextQuestion,
    RequestDraftData,
    UpdateSource,
)

TelegramExtractionMode = Literal["rule", "openai", "hybrid", "fake"]
TELEGRAM_INTAKE_PROMPT_VERSION = "approval-context+telegram-intake-v1"
TELEGRAM_INTAKE_SCHEMA_VERSION = "OpenAIApprovalExtractionPayload-v2"
_DETERMINISTIC_AUTHORITY_FIELDS = {
    "amount",
    "quantity",
    "unit",
    "budget_status",
    "desired_delivery_date",
}


@dataclass(frozen=True)
class IntakeExtractionResult:
    update: IntakeFieldUpdate
    proposed_fields: int = 0
    accepted_fields: int = 0
    rejected_fields: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IntakeProviderFailure:
    error: str
    reason: str
    error_type: str | None = None
    diagnostic_code: str | None = None
    validation_errors: tuple[str, ...] = ()
    validation_error_codes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedIntakeExtraction:
    update: IntakeFieldUpdate | None
    structured: IntakeExtractionResult | None = None
    provider_called: bool = False
    provider_succeeded: bool = False
    provider_failed: bool = False
    fallback_used: bool = False
    failure: IntakeProviderFailure | None = None


class TelegramIntakeExtractionService:
    """Application adapter over the existing structured extraction provider."""

    def __init__(
        self,
        provider: ApprovalExtractionProvider,
        *,
        date_parser: NaturalDateParser | None = None,
        min_confidence: float = 0.70,
    ) -> None:
        self._provider = provider
        self._dates = date_parser or NaturalDateParser()
        self._min_confidence = min_confidence

    def extract(
        self,
        text: str,
        draft: RequestDraftData | None,
        next_question: NextQuestion | None,
        *,
        source_kind: str,
    ) -> IntakeExtractionResult:
        started = perf_counter()
        raw = self._provider.extract(
            _compact_provider_input(text, draft, next_question, source_kind)
        )
        values, evidence, rejected = self._normalize(text, raw)
        provider_metadata = getattr(self._provider, "last_metadata", {})
        metadata = (
            dict(provider_metadata) if isinstance(provider_metadata, dict) else {}
        )
        metadata.update(
            {
                "duration_ms": int((perf_counter() - started) * 1000),
                "conflict_count": len(raw.contradictions),
                "prompt_version": TELEGRAM_INTAKE_PROMPT_VERSION,
                "schema_version": TELEGRAM_INTAKE_SCHEMA_VERSION,
            }
        )
        return IntakeExtractionResult(
            update=IntakeFieldUpdate(
                values=values,
                source=UpdateSource.EXTRACTION,
                evidence_by_field=evidence,
            ),
            proposed_fields=_proposed_count(raw),
            accepted_fields=len(values),
            rejected_fields=tuple(sorted(rejected)),
            metadata=metadata,
        )

    def resolve_message(
        self,
        text: str,
        draft: RequestDraftData | None,
        next_question: NextQuestion | None,
        deterministic: IntakeFieldUpdate,
        *,
        source_kind: str,
        merge_deterministic: bool,
        fallback_on_error: bool,
    ) -> ResolvedIntakeExtraction:
        """Resolve one provider call with the shared Telegram fallback policy."""
        try:
            structured = self.extract(
                text,
                draft,
                next_question,
                source_kind=source_kind,
            )
        except Exception as exc:
            return ResolvedIntakeExtraction(
                update=(
                    conservative_deterministic_fallback(deterministic)
                    if fallback_on_error
                    else None
                ),
                provider_called=True,
                provider_failed=True,
                fallback_used=fallback_on_error,
                failure=_safe_provider_failure(exc),
            )
        update = (
            merge_intake_candidates(deterministic, structured.update)
            if merge_deterministic
            else structured.update.model_copy(
                update={
                    "explicit_correction": deterministic.explicit_correction,
                    "corrections": deterministic.corrections,
                }
            )
        )
        return ResolvedIntakeExtraction(
            update=update,
            structured=structured,
            provider_called=True,
            provider_succeeded=True,
        )

    def _normalize(
        self,
        source_text: str,
        raw: RawApprovalExtraction,
    ) -> tuple[dict[str, object], dict[str, str], set[str]]:
        values: dict[str, object] = {}
        evidence: dict[str, str] = {}
        rejected: set[str] = set()

        candidates: dict[str, object | None] = {
            "procurement_type": raw.procurement_type_raw,
            "item_name": _clean_string(raw.item_name_raw),
            "specifications": _clean_string(raw.specifications_raw),
            "desired_result": _clean_string(raw.desired_result_raw),
            "delivery_location": _clean_string(raw.delivery_location_raw),
            "business_justification": _clean_string(
                raw.business_justification_raw
            ),
            "department": _clean_string(raw.department_raw),
            "contact_person": _clean_string(raw.contact_person_raw),
            "budget_status": raw.budget_status_raw,
        }
        for field_name, candidate in candidates.items():
            if candidate is None:
                continue
            if not _trusted(field_name, source_text, raw, self._min_confidence):
                rejected.add(field_name)
                continue
            values[field_name] = candidate
            _copy_evidence(field_name, raw, evidence)

        category = _clean_string(raw.category_raw)
        if category is not None:
            category = category.upper()
            if (
                category in CATEGORY_NAMES
                and _trusted("category", source_text, raw, self._min_confidence)
            ):
                values["category_code"] = category
                _copy_evidence(
                    "category",
                    raw,
                    evidence,
                    output_name="category_code",
                )
            else:
                rejected.add("category_code")

        if raw.quantity_raw and _trusted(
            "quantity", source_text, raw, self._min_confidence
        ):
            try:
                try:
                    values["quantity"] = Decimal(parse_cardinal(raw.quantity_raw))
                except ValueError:
                    values["quantity"] = Decimal(
                        raw.quantity_raw.replace(" ", "").replace(",", ".")
                    )
                _copy_evidence("quantity", raw, evidence)
            except InvalidOperation:
                rejected.add("quantity")
        if raw.unit_raw and _trusted("unit", source_text, raw, self._min_confidence):
            try:
                values["unit"] = normalize_unit(raw.unit_raw)
                _copy_evidence("unit", raw, evidence)
            except ValueError:
                rejected.add("unit")

        if raw.desired_delivery_date_raw and _trusted(
            "desired_delivery_date", source_text, raw, self._min_confidence
        ):
            try:
                values["desired_delivery_date"] = self._dates.parse(
                    raw.desired_delivery_date_raw
                )
                _copy_evidence("desired_delivery_date", raw, evidence)
            except ValueError:
                rejected.add("desired_delivery_date")

        if raw.amount_raw and _trusted(
            "amount", source_text, raw, self._min_confidence
        ):
            try:
                parsed = parse_amount_expression(raw.amount_raw)
                modifier = raw.amount_modifier_raw or parsed.modifier
                period = raw.billing_period_raw
                parsed = parsed.__class__(
                    amount=parsed.amount,
                    modifier=modifier,
                    billing_period=(
                        None if period in {None, "one_time"} else period
                    ),
                )
                values["amount"] = parsed.amount
                evidence["amount"] = amount_evidence(parsed)
            except ValueError:
                rejected.add("amount")

        _preserve_service_action(values, source_text)
        _remove_duplicate_desired_result(values, evidence, rejected)
        _remove_incompatible_category(values, rejected)
        if values.get("procurement_type") == "service":
            values.pop("quantity", None)
            values.pop("unit", None)
        return values, evidence, rejected


def merge_intake_candidates(
    deterministic: IntakeFieldUpdate,
    structured: IntakeFieldUpdate,
) -> IntakeFieldUpdate:
    values = dict(structured.values)
    evidence = dict(structured.evidence_by_field)
    for field_name in deterministic.suppressed_extraction_fields:
        values.pop(field_name, None)
        evidence.pop(field_name, None)
    deterministic_type = deterministic.values.get("procurement_type")
    structured_type = structured.values.get("procurement_type")
    deterministic_type_supported = (
        deterministic_type in {"goods", "service"}
        and bool(deterministic.evidence_by_field.get("procurement_type"))
    )
    if deterministic_type_supported:
        if structured_type in {None, deterministic_type}:
            values["procurement_type"] = deterministic_type
            evidence["procurement_type"] = deterministic.evidence_by_field[
                "procurement_type"
            ]
        elif structured_type in {"goods", "service"}:
            values.pop("procurement_type", None)
            values.pop("category_code", None)
            evidence.pop("procurement_type", None)
            evidence.pop("category_code", None)
    for field_name in _DETERMINISTIC_AUTHORITY_FIELDS:
        if field_name not in deterministic.values:
            continue
        if field_name == "unit" and _prefer_structured_explicit_unit(
            deterministic,
            structured,
        ):
            continue
        values[field_name] = deterministic.values[field_name]
        if field_name in deterministic.evidence_by_field:
            evidence[field_name] = deterministic.evidence_by_field[field_name]

    deterministic_location = deterministic.values.get("delivery_location")
    structured_location = values.get("delivery_location")
    preferred_location = _prefer_supported_location(
        structured_location,
        deterministic_location,
    )
    if preferred_location is not None:
        values["delivery_location"] = preferred_location
        if preferred_location == deterministic_location:
            location_evidence = deterministic.evidence_by_field.get(
                "delivery_location"
            )
            if location_evidence:
                evidence["delivery_location"] = location_evidence

    procurement_type = values.get("procurement_type")
    if "category_code" not in values and procurement_type in {"goods", "service"}:
        deterministic_category = deterministic.values.get("category_code")
        expected_prefix = "G" if procurement_type == "goods" else "S"
        if (
            isinstance(deterministic_category, str)
            and deterministic_category.startswith(expected_prefix)
            and deterministic_category in CATEGORY_NAMES
        ):
            values["category_code"] = deterministic_category
            category_evidence = deterministic.evidence_by_field.get(
                "category_code"
            )
            if category_evidence:
                evidence["category_code"] = category_evidence
    category = values.get("category_code")
    if isinstance(category, str):
        if procurement_type not in {"goods", "service"}:
            values.pop("category_code", None)
            evidence.pop("category_code", None)
        else:
            compatible_prefix = "G" if procurement_type == "goods" else "S"
            if not category.startswith(compatible_prefix):
                values.pop("category_code", None)
                evidence.pop("category_code", None)
    if procurement_type == "service":
        deterministic_specifications = deterministic.values.get("specifications")
        if isinstance(deterministic_specifications, str):
            values["specifications"] = _prefer_richer_supported_text(
                values.get("specifications"),
                deterministic_specifications,
            )
            if values["specifications"] == deterministic_specifications:
                specifications_evidence = deterministic.evidence_by_field.get(
                    "specifications"
                )
                if specifications_evidence:
                    evidence["specifications"] = specifications_evidence
        values.pop("quantity", None)
        values.pop("unit", None)
        evidence.pop("quantity", None)
        evidence.pop("unit", None)
    return IntakeFieldUpdate(
        values=values,
        source=UpdateSource.EXTRACTION,
        evidence_by_field=evidence,
        explicit_correction=deterministic.explicit_correction,
        corrections=deterministic.corrections,
        suppressed_extraction_fields=deterministic.suppressed_extraction_fields,
    )


def conservative_deterministic_fallback(
    deterministic: IntakeFieldUpdate,
) -> IntakeFieldUpdate:
    values = {
        field_name: value
        for field_name, value in deterministic.values.items()
        if field_name in _DETERMINISTIC_AUTHORITY_FIELDS
    }
    if (
        deterministic.values.get("procurement_type") in {"goods", "service"}
        and deterministic.evidence_by_field.get("procurement_type")
    ):
        values["procurement_type"] = deterministic.values["procurement_type"]
    evidence = {
        field_name: value
        for field_name, value in deterministic.evidence_by_field.items()
        if field_name in values
    }
    if "unit" in values and "unit" not in evidence:
        values.pop("unit")
    return IntakeFieldUpdate(
        values=values,
        source=UpdateSource.EXTRACTION,
        evidence_by_field=evidence,
        explicit_correction=deterministic.explicit_correction,
        corrections=[
            item
            for item in deterministic.corrections
            if item.target_field in values
        ],
        suppressed_extraction_fields=deterministic.suppressed_extraction_fields,
    )


def _compact_provider_input(
    text: str,
    draft: RequestDraftData | None,
    question: NextQuestion | None,
    source_kind: str,
) -> str:
    allowed_context_fields = (
        "procurement_type",
        "category_code",
        "item_name",
        "quantity",
        "unit",
        "specifications",
        "desired_result",
        "amount",
        "budget_status",
        "desired_delivery_date",
        "delivery_location",
        "business_justification",
    )
    context = {
        field_name: draft.model_dump(mode="json").get(field_name)
        for field_name in allowed_context_fields
        if draft is not None and getattr(draft, field_name) is not None
    }
    payload = {
        "source_kind": source_kind,
        "current_question": question.text if question else None,
        "awaiting_field": question.field_code if question else None,
        "confirmed_context": context,
        "user_message": text,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _prefer_supported_location(
    primary: object,
    candidate: object,
) -> str | None:
    if not isinstance(primary, str):
        return candidate if isinstance(candidate, str) else None
    if not isinstance(candidate, str):
        return primary
    primary_normalized = _normalized_text(primary)
    candidate_normalized = _normalized_text(candidate)
    if primary_normalized in candidate_normalized:
        return candidate
    if candidate_normalized in primary_normalized:
        return primary
    return primary


def _prefer_structured_explicit_unit(
    deterministic: IntakeFieldUpdate,
    structured: IntakeFieldUpdate,
) -> bool:
    return (
        deterministic.values.get("unit") == "шт."
        and not _has_explicit_unit_evidence(deterministic)
        and _has_explicit_unit_evidence(structured)
    )


def _has_explicit_unit_evidence(update: IntakeFieldUpdate) -> bool:
    unit = update.values.get("unit")
    evidence = update.evidence_by_field.get("unit")
    if not isinstance(unit, str) or not evidence:
        return False
    tokens = re.findall(r"[a-zа-яё².]+", evidence.casefold())
    candidates = [*tokens]
    candidates.extend(
        " ".join(tokens[index : index + 2])
        for index in range(len(tokens) - 1)
    )
    for candidate in candidates:
        try:
            if normalize_unit(candidate) == unit:
                return True
        except ValueError:
            continue
    return False


def _prefer_richer_supported_text(primary: object, candidate: str) -> str:
    if not isinstance(primary, str) or not primary.strip():
        return candidate
    primary_normalized = _normalized_text(primary)
    candidate_normalized = _normalized_text(candidate)
    if primary_normalized in candidate_normalized:
        return candidate
    if candidate_normalized in primary_normalized:
        return primary
    return f"{primary.rstrip(' .;')}; {candidate}"


def _normalized_text(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    return " ".join(re.findall(r"[0-9a-zа-я]+", normalized))


def _trusted(
    field_name: str,
    source_text: str,
    raw: RawApprovalExtraction,
    minimum: float,
) -> bool:
    score = raw.confidence_by_field.get(field_name)
    if score is not None and score < minimum:
        return False
    evidence = raw.evidence_by_field.get(field_name)
    return bool(
        evidence and evidence_supports_field(field_name, source_text, evidence)
    )


def _copy_evidence(
    field_name: str,
    raw: RawApprovalExtraction,
    destination: dict[str, str],
    *,
    output_name: str | None = None,
) -> None:
    value = raw.evidence_by_field.get(field_name)
    if value:
        destination[output_name or field_name] = value


def _clean_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip(" ,.;")
    return normalized or None


def _remove_incompatible_category(
    values: dict[str, object],
    rejected: set[str],
) -> None:
    procurement_type = values.get("procurement_type")
    category = values.get("category_code")
    if not isinstance(category, str):
        return
    if procurement_type not in {"goods", "service"}:
        values.pop("category_code", None)
        rejected.add("category_code")
        return
    expected = "G" if procurement_type == "goods" else "S"
    if not category.startswith(expected):
        values.pop("category_code", None)
        rejected.add("category_code")


def _preserve_service_action(
    values: dict[str, object],
    source_text: str,
) -> None:
    item = values.get("item_name")
    if values.get("procurement_type") != "service" or not isinstance(item, str):
        return
    item = normalize_service_item_name(item)
    values["item_name"] = item
    normalized_item = item.casefold().replace("ё", "е")
    source = source_text.casefold().replace("ё", "е")
    actions = (
        (r"\b(?:установить|установка|монтаж)\w*", "установка", ()),
        (r"\b(?:настроить|настройка)\w*", "настройка", ()),
        (r"\b(?:отремонтировать|ремонт)\w*", "ремонт", ()),
        (r"\b(?:собрать|сборка)\w*", "сборка", ()),
        (r"\b(?:обслужить|обслуживание)\w*", "обслуживание", ()),
        (r"\b(?:разработать|разработка)\w*", "разработка", ()),
        (r"\b(?:доставить|доставка)\w*", "доставка", ()),
        (r"\b(?:убрать|уборка|клининг)\w*", "уборка", ("клининг",)),
    )
    for pattern, label, synonyms in actions:
        has_action = label in normalized_item or any(
            synonym in normalized_item for synonym in synonyms
        )
        if re.search(pattern, source) and not has_action:
            values["item_name"] = f"{label} {_to_genitive_object(item)}"
            return


_SERVICE_ACTION_NOUNS = {
    "установить": "установка",
    "настроить": "настройка",
    "разработать": "разработка",
    "организовать": "организация",
    "отремонтировать": "ремонт",
    "собрать": "сборка",
    "обслужить": "обслуживание",
    "доставить": "доставка",
    "убрать": "уборка",
}


def normalize_service_item_name(value: str) -> str:
    """Turn duplicated action labels into a short natural service name."""
    normalized = " ".join(value.strip().split())
    parts = re.split(r"\s+[—–-]\s+", normalized, maxsplit=1)
    if len(parts) == 2:
        label, tail = parts
        converted = _action_phrase(tail, expected_label=label)
        if converted is not None:
            return converted
        if label.casefold() == "уборка" and tail.casefold().startswith("клининг"):
            return tail
        return normalized
    return _action_phrase(normalized) or normalized


def _remove_duplicate_desired_result(
    values: dict[str, object],
    evidence: dict[str, str],
    rejected: set[str],
) -> None:
    item = values.get("item_name")
    desired = values.get("desired_result")
    if not isinstance(item, str) or not isinstance(desired, str):
        return
    normalized_item = _normalized_service_text(
        normalize_service_item_name(item)
    )
    normalized_desired = _normalized_service_text(
        normalize_service_item_name(desired)
    )
    if normalized_item == normalized_desired:
        values.pop("desired_result", None)
        evidence.pop("desired_result", None)
        rejected.add("desired_result")


def _normalized_service_text(value: str) -> str:
    return " ".join(
        re.findall(r"[a-zа-я0-9]+", value.casefold().replace("ё", "е"))
    )


def _action_phrase(value: str, *, expected_label: str | None = None) -> str | None:
    match = re.match(r"^(?P<verb>[А-Яа-яЁё]+)\s+(?P<object>.+)$", value)
    if match is None:
        return None
    label = _SERVICE_ACTION_NOUNS.get(
        match.group("verb").casefold().replace("ё", "е")
    )
    if label is None:
        return None
    if expected_label is not None and (
        expected_label.casefold().replace("ё", "е") != label
    ):
        return None
    return f"{label} {_to_genitive_object(match.group('object'))}"


def _to_genitive_object(value: str) -> str:
    first, separator, rest = value.partition(" ")
    lower = first.casefold().replace("ё", "е")
    if lower.endswith("ию"):
        converted = first[:-2] + "ии"
    elif lower.endswith("ую"):
        converted = first[:-2] + "ой"
    elif lower.endswith("ие"):
        converted = first[:-2] + "ия"
    elif lower.endswith("ье"):
        converted = first[:-2] + "ья"
    elif lower.endswith("у"):
        ending = "и" if lower[-2:-1] in "гкхжчшщц" else "ы"
        converted = first[:-1] + ending
    elif lower.endswith(("я", "ь")):
        converted = first[:-1] + "и"
    elif lower.endswith("ы"):
        converted = first[:-1] + "ов"
    elif lower[-1:] in "бвгджзклмнпрстфхцчшщ":
        converted = first + "а"
    else:
        converted = first
    return converted + (separator + rest if separator else "")


def _proposed_count(raw: RawApprovalExtraction) -> int:
    fields = (
        raw.procurement_type_raw,
        raw.item_name_raw,
        raw.quantity_raw,
        raw.unit_raw,
        raw.specifications_raw,
        raw.desired_result_raw,
        raw.amount_raw,
        raw.budget_status_raw,
        raw.category_raw,
        raw.desired_delivery_date_raw,
        raw.delivery_location_raw,
        raw.business_justification_raw,
        raw.department_raw,
        raw.contact_person_raw,
    )
    return sum(value is not None for value in fields)


def _safe_provider_failure(exc: Exception) -> IntakeProviderFailure:
    if isinstance(exc, ApprovalExtractionProviderError):
        return IntakeProviderFailure(
            error=type(exc).__name__,
            reason=exc.safe_message,
            error_type=exc.error_type,
            diagnostic_code=exc.diagnostic_code,
            validation_errors=tuple(exc.validation_errors or ()),
            validation_error_codes=dict(exc.validation_error_codes or {}),
        )
    return IntakeProviderFailure(
        error=type(exc).__name__,
        reason="Structured extraction provider is unavailable",
    )
