from dataclasses import dataclass

from app.extraction.models import (
    ExtractionStatus,
    NormalizedApprovalExtraction,
    RawApprovalExtraction,
)
from app.extraction.normalization import (
    MultipleIndependentAmountsError,
    MultipleMoneyRangesError,
    category_candidates,
    evidence_is_present,
    fact_requires_evidence,
    match_category,
    normalize_budget_status,
    normalize_money,
    normalize_urgency,
)
from app.rules.models import ApprovalContext

MISSING_QUESTIONS = {
    "amount": "Укажите сумму закупки.",
    "budget_status": (
        "Закупка предусмотрена бюджетом или является внебюджетной?"
    ),
    "category_code": "Уточните тип закупки.",
}
RANGE_QUESTION = (
    "Какую сумму использовать для определения маршрута согласования: "
    "ожидаемую или максимальную?"
)
MULTIPLE_AMOUNTS_CONTRADICTION = (
    "Обнаружено несколько независимых сумм без указания их ролей"
)
REQUIRED_CONTEXT_FIELDS = {"amount", "budget_status"}
CATEGORY_CODES_WITH_ADDITIONAL_RULES = {"S11"}


@dataclass(frozen=True)
class ApprovalPostProcessingResult:
    status: ExtractionStatus
    extraction: NormalizedApprovalExtraction
    approval_context: ApprovalContext | None
    clarification_questions: list[str]


class ApprovalExtractionPostProcessor:
    def __init__(self, *, min_confidence: float = 0.70) -> None:
        self._min_confidence = min_confidence

    def process(
        self,
        source_text: str,
        raw: RawApprovalExtraction,
    ) -> ApprovalPostProcessingResult:
        contradictions: list[str] = []
        warnings: list[str] = []
        missing = [
            field
            for field in dict.fromkeys(raw.unknown_fields)
            if field in REQUIRED_CONTEXT_FIELDS
        ]
        valid_evidence, invalid_fields, evidence_conflicts = (
            _validate_evidence(source_text, raw)
        )
        contradictions.extend(evidence_conflicts)

        money = None
        amount = None
        if "amount" not in invalid_fields:
            money_source = source_text
            try:
                money = normalize_money(money_source)
            except (MultipleIndependentAmountsError, MultipleMoneyRangesError):
                missing.append("amount")
                contradictions.append(MULTIPLE_AMOUNTS_CONTRADICTION)
            except ValueError:
                if raw.amount_raw:
                    try:
                        money = normalize_money(raw.amount_raw)
                    except (
                        MultipleIndependentAmountsError,
                        MultipleMoneyRangesError,
                    ):
                        missing.append("amount")
                        contradictions.append(
                            MULTIPLE_AMOUNTS_CONTRADICTION
                        )
                    except ValueError as exc:
                        missing.append("amount")
                        warnings.append(str(exc))
                else:
                    missing.append("amount")
            if money is not None:
                if money.amount_type == "range":
                    missing.append("amount")
                else:
                    amount = money.amount
                    if money.amount_type == "maximum":
                        warnings.append(
                            "Указана верхняя граница, а не точная сумма"
                        )
                    elif money.amount_type == "approximate":
                        warnings.append("Указана приблизительная сумма")
        else:
            missing.append("amount")

        budget_status, budget_conflicts = normalize_budget_status(source_text)
        contradictions.extend(budget_conflicts)
        if budget_conflicts:
            budget_status = None
            missing.append("budget_status")
        elif "budget_status" in invalid_fields:
            budget_status = None
        elif budget_status is None and raw.budget_status_raw in {
            "budgeted",
            "unbudgeted",
        }:
            budget_status = raw.budget_status_raw
        if budget_status is None:
            missing.append("budget_status")

        urgency, urgency_claimed, urgency_warnings = normalize_urgency(
            source_text
        )
        if "urgency" in invalid_fields:
            urgency = None
        elif urgency is None and raw.urgency_raw:
            candidate = raw.urgency_raw.strip().upper()
            if candidate in {"P1", "P2", "P3", "P4"}:
                urgency = candidate
        urgency_claimed = urgency_claimed or raw.urgency_claimed
        warnings.extend(urgency_warnings)

        category_code, category_warnings = match_category(source_text)
        if category_warnings:
            category_code = None
            if (
                category_candidates(source_text)
                & CATEGORY_CODES_WITH_ADDITIONAL_RULES
            ):
                missing.append("category_code")
        elif category_code is None and raw.category_raw:
            category_code, raw_category_warnings = match_category(
                raw.category_raw
            )
            category_warnings.extend(raw_category_warnings)
        if "category" in invalid_fields:
            category_code = None
        warnings.extend(category_warnings)

        single_supplier = (
            raw.single_supplier_raw is True
            and "single_supplier" not in invalid_fields
        )
        has_data_access = (
            raw.has_data_access_raw is True
            and "has_data_access" not in invalid_fields
        )
        work_on_site = (
            raw.work_on_site_raw is True
            and "work_on_site" not in invalid_fields
        )

        for field, score in raw.confidence_by_field.items():
            if score < self._min_confidence:
                warnings.append(
                    f"Недостаточная уверенность для поля {field}"
                )
                if (
                    field in REQUIRED_CONTEXT_FIELDS
                    and field not in missing
                ):
                    missing.append(field)

        warnings.extend(
            _provider_contradiction_warnings(
                raw.contradictions,
                contradictions,
            )
        )

        missing = list(dict.fromkeys(missing))
        contradictions = list(dict.fromkeys(contradictions))
        warnings = list(dict.fromkeys(warnings))
        questions = [
            RANGE_QUESTION
            if field == "amount" and money and money.amount_type == "range"
            else MISSING_QUESTIONS[field]
            for field in missing
            if field in MISSING_QUESTIONS
            or (field == "amount" and money and money.amount_type == "range")
        ]
        extraction = NormalizedApprovalExtraction(
            amount=amount,
            budget_status=budget_status,
            urgency=urgency,
            urgency_claimed=urgency_claimed,
            single_supplier=single_supplier,
            category_code=category_code,
            has_data_access=has_data_access,
            work_on_site=work_on_site,
            confidence_by_field=raw.confidence_by_field,
            evidence_by_field=valid_evidence,
            missing_fields=missing,
            contradictions=contradictions,
            warnings=warnings,
            source_text=source_text,
            money=money,
        )
        status = _status(extraction)
        context = _approval_context(extraction) if status == "extracted" else None
        return ApprovalPostProcessingResult(
            status=status,
            extraction=extraction,
            approval_context=context,
            clarification_questions=questions,
        )


def _validate_evidence(
    source_text: str,
    raw: RawApprovalExtraction,
) -> tuple[dict[str, str], set[str], list[str]]:
    valid: dict[str, str] = {}
    invalid: set[str] = set()
    contradictions: list[str] = []
    for field, evidence in raw.evidence_by_field.items():
        if evidence and evidence_is_present(source_text, evidence):
            valid[field] = evidence
        else:
            invalid.add(field)
            contradictions.append(
                f"Evidence для поля {field} отсутствует в исходном тексте"
            )
    facts = {
        "amount": raw.amount_raw,
        "budget_status": raw.budget_status_raw,
        "urgency": raw.urgency_raw
        or ("claimed" if raw.urgency_claimed else None),
        "single_supplier": raw.single_supplier_raw,
        "category": raw.category_raw,
        "has_data_access": raw.has_data_access_raw,
        "work_on_site": raw.work_on_site_raw,
    }
    for field, value in facts.items():
        if fact_requires_evidence(field, value) and field not in valid:
            invalid.add(field)
            contradictions.append(
                f"Для поля {field} отсутствует подтверждающее evidence"
            )
    return valid, invalid, contradictions


def _provider_contradiction_warnings(
    values: list[str],
    confirmed: list[str],
) -> list[str]:
    return [
        "Неподтверждённое замечание provider: "
        + value.replace("\r", " ").replace("\n", " ").strip()[:200]
        for value in values
        if value.strip() and not _matches_confirmed_contradiction(
            value,
            confirmed,
        )
    ]


def _matches_confirmed_contradiction(
    provider_value: str,
    confirmed: list[str],
) -> bool:
    note = provider_value.casefold().replace("ё", "е")
    normalized_confirmed = [
        value.casefold().replace("ё", "е") for value in confirmed
    ]
    if any(value in note or note in value for value in normalized_confirmed):
        return True
    if "Противоречивые сведения о бюджетном статусе" in confirmed:
        has_budget = "бюджет" in note or "budget" in note
        has_conflict = any(
            marker in note
            for marker in (
                "противореч",
                "одноврем",
                "conflict",
                "both",
            )
        ) or ("budgeted" in note and "unbudgeted" in note)
        if has_budget and has_conflict:
            return True
    if MULTIPLE_AMOUNTS_CONTRADICTION in confirmed:
        has_amount = "сумм" in note or "amount" in note
        has_multiple = any(
            marker in note
            for marker in ("нескольк", "multiple", "different")
        )
        if has_amount and has_multiple:
            return True
    return False


def _status(extraction: NormalizedApprovalExtraction) -> ExtractionStatus:
    if extraction.contradictions:
        return "conflict"
    if extraction.missing_fields:
        return "needs_clarification"
    return "extracted"


def _approval_context(
    extraction: NormalizedApprovalExtraction,
) -> ApprovalContext:
    assert extraction.amount is not None
    return ApprovalContext(
        amount=extraction.amount,
        budget_status=extraction.budget_status,
        urgency=extraction.urgency,
        single_supplier=extraction.single_supplier,
        category_code=extraction.category_code,
        has_data_access=extraction.has_data_access,
        work_on_site=extraction.work_on_site,
    )
