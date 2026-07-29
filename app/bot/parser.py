import re
from decimal import Decimal, InvalidOperation

from app.bot.categories import DeterministicCategoryClassifier
from app.bot.extraction import DeterministicEntityExtractor
from app.bot.normalization import (
    NaturalDateParser,
    UnknownIntakeValueError,
    amount_evidence,
    normalize_budget_status,
    normalize_procurement_type,
    normalize_unit,
    parse_amount_expression,
    parse_cardinal,
)
from app.intake.field_registry import CATEGORY_NAMES, RequestFieldRegistry
from app.intake.models import IntakeFieldUpdate, NextQuestion
from app.intake.validators import IntakeFieldValidator

_SPACES = re.compile(r"[\s\u00a0]+")
_CATEGORY_CODE = re.compile(r"^([A-Za-z]\d{2})(?:\s*[—–-].*)?$", re.DOTALL)


class TelegramParseError(ValueError):
    """A safe validation hint that can be returned to a Telegram user."""


class DeterministicIntakeParser:
    def __init__(
        self,
        registry: RequestFieldRegistry | None = None,
        validator: IntakeFieldValidator | None = None,
        category_classifier: DeterministicCategoryClassifier | None = None,
        date_parser: NaturalDateParser | None = None,
        entity_extractor: DeterministicEntityExtractor | None = None,
    ) -> None:
        self._registry = registry or RequestFieldRegistry()
        self._validator = validator or IntakeFieldValidator(self._registry)
        self._categories = category_classifier or DeterministicCategoryClassifier()
        self._dates = date_parser or NaturalDateParser()
        self._extractor = entity_extractor or DeterministicEntityExtractor(
            self._categories,
            self._dates,
        )

    def parse(
        self,
        text: str,
        next_question: NextQuestion | None = None,
        awaiting_field_code: str | None = None,
        category_candidates: tuple[str, ...] = (),
    ) -> IntakeFieldUpdate:
        value = text.strip()
        if not value:
            raise TelegramParseError("Отправьте непустой текстовый ответ.")
        field_code = (
            next_question.field_code
            if next_question is not None
            else awaiting_field_code
        )
        if field_code is None:
            return self._extractor.extract(value).to_update()

        definition = self._registry.get(field_code)
        if definition is None:
            raise TelegramParseError(
                "Не удалось определить ожидаемый вопрос. Повторите команду /start."
            )
        try:
            evidence: dict[str, str] = {}
            if field_code == "amount":
                amount = parse_amount_expression(value)
                parsed = amount.amount
                evidence["amount"] = amount_evidence(amount)
            else:
                parsed = self._parse_value(
                    field_code,
                    definition.question_type,
                    value,
                    category_candidates,
                )
            normalized = self._validator.normalize(field_code, parsed)
        except UnknownIntakeValueError as exc:
            raise TelegramParseError(str(exc)) from exc
        except (InvalidOperation, ValueError) as exc:
            raise TelegramParseError(self._format_hint(field_code)) from exc
        return IntakeFieldUpdate(
            values={field_code: normalized},
            evidence_by_field=evidence,
        )

    def _parse_value(
        self,
        field_code: str,
        question_type: str,
        value: str,
        category_candidates: tuple[str, ...],
    ):
        if field_code == "quantity":
            normalized = " ".join(value.casefold().replace("ё", "е").split())
            try:
                return Decimal(parse_cardinal(normalized))
            except ValueError:
                compact = _SPACES.sub("", value).replace(",", ".")
                try:
                    return Decimal(compact)
                except InvalidOperation:
                    extracted = self._extractor.extract(value).values.get(
                        "quantity"
                    )
                    if extracted is None:
                        raise
                    return extracted
        if field_code == "unit":
            return normalize_unit(value)
        if field_code == "desired_delivery_date":
            found = self._dates.search(value)
            return found[0] if found is not None else self._dates.parse(value)
        if field_code == "procurement_type":
            return normalize_procurement_type(value)
        if field_code == "budget_status":
            return normalize_budget_status(value)
        if field_code == "category_code":
            return self._parse_category(value, category_candidates)
        if question_type == "boolean":
            return value
        return value

    def _parse_category(
        self,
        value: str,
        candidates: tuple[str, ...],
    ) -> str:
        normalized = " ".join(value.split())
        if normalized.isdigit() and candidates:
            index = int(normalized) - 1
            if 0 <= index < len(candidates):
                return candidates[index]
            raise ValueError("Category candidate number is out of range")
        code_match = _CATEGORY_CODE.fullmatch(normalized)
        if code_match is not None:
            code = code_match.group(1).upper()
            if code in CATEGORY_NAMES:
                return code
            raise ValueError("Unknown category code")
        by_name = self._categories.category_by_name(normalized)
        if by_name is not None:
            return by_name
        classification = self._categories.classify(normalized)
        if classification.kind == "exact" and classification.category_code:
            return classification.category_code
        raise ValueError("Category is ambiguous or unknown")

    @staticmethod
    def _format_hint(field_code: str) -> str:
        if field_code == "desired_delivery_date":
            return (
                "Не удалось точно определить дату. Напишите, например: "
                "20 августа, через 10 дней или 2026-08-20."
            )
        if field_code == "amount":
            return (
                "Не удалось определить сумму. Напишите, например: "
                "500 ₽, 500 руб., 500 р. или не более 120 тыс. руб."
            )
        if field_code == "quantity":
            return "Напишите количество числом, например 10."
        if field_code == "unit":
            return "Укажите единицу, например: шт., кг, м, м², час или день."
        if field_code == "category_code":
            return "Напишите номер варианта или название категории."
        if field_code == "procurement_type":
            return "Ответьте: «Товар» или «Услуга»."
        if field_code == "budget_status":
            return "Ответьте, предусмотрена ли закупка в утверждённом бюджете."
        return "Проверьте ответ и попробуйте ещё раз."
