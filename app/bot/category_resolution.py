import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from time import perf_counter
from typing import Literal, Protocol

from openai import OpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.bot.categories import (
    CategoryClassification,
    DeterministicCategoryClassifier,
)
from app.extraction.normalization import evidence_supports_field
from app.intake.field_registry import (
    CATEGORY_DESCRIPTIONS,
    CATEGORY_NAMES,
    CATEGORY_TAXONOMY_VERSION,
)
from app.intake.models import IntakeFieldUpdate, RequestDraftData

logger = logging.getLogger(__name__)

CategoryDecision = Literal["exact", "candidates", "unresolved"]
ResolutionDecision = Literal[
    "deterministic_exact",
    "deterministic_candidates",
    "llm_exact",
    "llm_candidates",
    "unresolved",
]
CategoryConfidence = Literal["high", "medium", "low"]
CategoryRationaleCode = Literal[
    "taxonomy_match",
    "ambiguous_taxonomy_match",
    "insufficient_context",
]

CategoryCode = Enum(
    "CategoryCode",
    {code: code for code in CATEGORY_NAMES},
    type=str,
)


class CategoryClassificationPayload(BaseModel):
    """Strict transport DTO for closed-taxonomy category classification."""

    model_config = ConfigDict(extra="forbid")

    decision: CategoryDecision
    primary_category_code: CategoryCode | None
    alternatives: list[CategoryCode] = Field(max_length=3)
    confidence: CategoryConfidence
    evidence: str | None
    rationale_code: CategoryRationaleCode

    @model_validator(mode="after")
    def validate_decision_shape(self):
        alternatives = [item.value for item in self.alternatives]
        if len(alternatives) != len(set(alternatives)):
            raise ValueError("category alternatives must be unique")
        if self.decision == "exact":
            if self.primary_category_code is None or alternatives:
                raise ValueError("exact decision requires one primary category")
        elif self.decision == "candidates":
            if (
                self.primary_category_code is not None
                or not 2 <= len(alternatives) <= 3
            ):
                raise ValueError("candidates decision requires 2-3 alternatives")
        elif self.primary_category_code is not None or alternatives:
            raise ValueError("unresolved decision cannot contain categories")
        return self


@dataclass(frozen=True)
class CategoryTaxonomyItem:
    code: str
    name: str
    description: str


@dataclass(frozen=True)
class CategoryClassificationRequest:
    procurement_type: Literal["goods", "service"]
    item_name: str
    source_text: str
    taxonomy_version: str
    taxonomy: tuple[CategoryTaxonomyItem, ...]


@dataclass(frozen=True)
class CategoryResolutionContext:
    """PII-minimised semantic input assembled from the canonical draft."""

    procurement_type: Literal["goods", "service"]
    item_name: str
    description: str | None = None
    specifications: str | None = None
    desired_result: str | None = None
    business_purpose: str | None = None
    current_subject_text: str | None = None

    @property
    def source_text(self) -> str:
        values = (
            self.item_name,
            self.description,
            self.specifications,
            self.desired_result,
            self.business_purpose,
            self.current_subject_text,
        )
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            compact = " ".join(value.split())
            normalized = _normalize(compact)
            if normalized and normalized not in seen:
                unique.append(compact)
                seen.add(normalized)
        return ". ".join(unique)

    @property
    def fingerprint(self) -> str:
        normalized = _normalize(self.source_text)
        return sha256(
            f"{self.procurement_type}:{normalized}".encode()
        ).hexdigest()[:16]


def build_category_resolution_context(
    draft: RequestDraftData | None,
    update: IntakeFieldUpdate,
    current_text: str,
    *,
    include_current_text: bool,
) -> CategoryResolutionContext | None:
    """Build category input from prospective canonical values, not one turn."""

    values = update.values
    procurement_type = values.get("procurement_type")
    if procurement_type not in {"goods", "service"} and draft is not None:
        procurement_type = (
            draft.procurement_type.value if draft.procurement_type is not None else None
        )
    item_name = values.get("item_name")
    if not isinstance(item_name, str) and draft is not None:
        item_name = draft.item_name
    if procurement_type not in {"goods", "service"} or not isinstance(
        item_name, str
    ):
        return None

    def prospective(field_name: str) -> str | None:
        value = values.get(field_name)
        if isinstance(value, str):
            return value
        if draft is not None:
            existing = getattr(draft, field_name)
            return existing if isinstance(existing, str) else None
        return None

    return CategoryResolutionContext(
        procurement_type=procurement_type,
        item_name=item_name,
        description=prospective("description"),
        specifications=prospective("specifications"),
        desired_result=prospective("desired_result"),
        business_purpose=prospective("business_justification"),
        current_subject_text=current_text if include_current_text else None,
    )


class CategoryClassificationProvider(Protocol):
    def classify(
        self, request: CategoryClassificationRequest
    ) -> CategoryClassificationPayload: ...


@dataclass(frozen=True)
class CategoryResolution:
    decision: ResolutionDecision
    category_code: str | None = None
    candidates: tuple[str, ...] = ()
    candidate_source: str | None = None
    requires_confirmation: bool = False
    provider_called: bool = False
    provider_failed: bool = False
    reason_code: str | None = None
    subject_fingerprint: str | None = None
    context_fingerprint: str | None = None


class OpenAICategoryClassificationProvider:
    """Uses the application's shared OpenAI client with a strict category DTO."""

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float,
        client: OpenAI,
    ) -> None:
        self.model = model
        self._timeout_seconds = timeout_seconds
        self._client = client

    def classify(
        self, request: CategoryClassificationRequest
    ) -> CategoryClassificationPayload:
        payload = {
            "procurement_type": request.procurement_type,
            "item_name": request.item_name,
            "source_text": request.source_text,
            "taxonomy_version": request.taxonomy_version,
            "allowed_taxonomy": [item.__dict__ for item in request.taxonomy],
        }
        response = self._client.responses.parse(
            model=self.model,
            instructions=(
                "Classify the procurement subject only against allowed_taxonomy. "
                "Return exact only when one category is clearly supported; return "
                "candidates only with 2-3 plausible alternatives. If exactly one "
                "category is plausible, use exact and put it in primary_category_code; "
                "never return candidates with fewer than 2 alternatives. Otherwise "
                "return unresolved. "
                "Evidence must be a short verbatim fragment of source_text. Never "
                "invent codes or return a category of another procurement type."
            ),
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            text_format=CategoryClassificationPayload,
            store=False,
            timeout=self._timeout_seconds,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise RuntimeError("Category provider returned no parsed output")
        return parsed


class FakeCategoryClassificationProvider:
    def __init__(
        self,
        payload: CategoryClassificationPayload | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.requests: list[CategoryClassificationRequest] = []

    def classify(
        self, request: CategoryClassificationRequest
    ) -> CategoryClassificationPayload:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.payload is None:
            raise RuntimeError("Fake category payload is not configured")
        return self.payload


class CategoryResolutionService:
    def __init__(
        self,
        classifier: DeterministicCategoryClassifier | None = None,
        provider: CategoryClassificationProvider | None = None,
    ) -> None:
        self._classifier = classifier or DeterministicCategoryClassifier()
        self._provider = provider

    def resolve(
        self,
        procurement_type: Literal["goods", "service"],
        item_name: str,
        source_text: str,
        *,
        context_fingerprint: str | None = None,
    ) -> CategoryResolution:
        started = perf_counter()
        combined_text = " ".join(
            value for value in (item_name, source_text) if value
        )
        deterministic = self._classifier.classify(
            combined_text,
            procurement_type,
        )
        deterministic = self._require_item_name_support(
            deterministic, procurement_type, item_name
        )
        fingerprint = category_subject_fingerprint(procurement_type, item_name)
        if deterministic.kind == "exact" and deterministic.category_code:
            return CategoryResolution(
                "deterministic_exact",
                category_code=deterministic.category_code,
                candidate_source="classifier_exact",
                subject_fingerprint=fingerprint,
                context_fingerprint=context_fingerprint,
            )
        if deterministic.candidates:
            return CategoryResolution(
                "deterministic_candidates",
                candidates=deterministic.candidates[:4],
                candidate_source="classifier_multiple",
                requires_confirmation=True,
                subject_fingerprint=fingerprint,
                context_fingerprint=context_fingerprint,
            )
        if self._provider is None:
            return CategoryResolution(
                "unresolved",
                reason_code="provider_unavailable",
                subject_fingerprint=fingerprint,
                context_fingerprint=context_fingerprint,
            )
        request = CategoryClassificationRequest(
            procurement_type=procurement_type,
            item_name=item_name,
            source_text=source_text,
            taxonomy_version=CATEGORY_TAXONOMY_VERSION,
            taxonomy=category_taxonomy(procurement_type),
        )
        raw_decision = "error"
        raw_codes: tuple[str, ...] = ()
        try:
            payload = self._provider.classify(request)
        except Exception:
            result = CategoryResolution(
                "unresolved",
                provider_called=True,
                provider_failed=True,
                reason_code="provider_failure",
                subject_fingerprint=fingerprint,
                context_fingerprint=context_fingerprint,
            )
            self._log_resolution(
                procurement_type, context_fingerprint, deterministic, raw_decision,
                raw_codes, result, started,
            )
            return result
        raw_decision = str(getattr(payload, "decision", "malformed"))
        try:
            raw_codes = _payload_codes(payload)
        except (AttributeError, TypeError, ValueError):
            raw_codes = ()
        try:
            validated = self.validate_provider_payload(
                procurement_type=procurement_type,
                source_text=source_text,
                deterministic=deterministic,
                payload=payload,
            )
        except (AttributeError, TypeError, ValueError):
            result = CategoryResolution(
                "unresolved",
                provider_called=True,
                reason_code="malformed_provider_result",
                subject_fingerprint=fingerprint,
                context_fingerprint=context_fingerprint,
            )
            self._log_resolution(
                procurement_type, context_fingerprint, deterministic, raw_decision,
                raw_codes, result, started,
            )
            return result
        result = CategoryResolution(
            **{
                **validated.__dict__,
                "provider_called": True,
                "subject_fingerprint": fingerprint,
                "context_fingerprint": context_fingerprint,
            }
        )
        self._log_resolution(
            procurement_type, context_fingerprint, deterministic, raw_decision,
            raw_codes, result, started,
        )
        return result

    @staticmethod
    def _log_resolution(
        procurement_type: str,
        context_fingerprint: str | None,
        deterministic: CategoryClassification,
        raw_decision: str,
        raw_codes: tuple[str, ...],
        result: CategoryResolution,
        started: float,
    ) -> None:
        logger.info(
            "Category resolution type=%s context_fingerprint=%s "
            "taxonomy_version=%s deterministic=%s provider_called=%s "
            "raw_decision=%s raw_codes=%s validated_decision=%s "
            "reason_code=%s candidate_codes=%s latency_ms=%s",
            procurement_type,
            context_fingerprint,
            CATEGORY_TAXONOMY_VERSION,
            deterministic.kind,
            result.provider_called,
            raw_decision,
            raw_codes,
            result.decision,
            result.reason_code,
            result.candidates
            or ((result.category_code,) if result.category_code else ()),
            int((perf_counter() - started) * 1000),
        )

    def _require_item_name_support(
        self,
        classification: CategoryClassification,
        procurement_type: Literal["goods", "service"],
        item_name: str,
    ) -> CategoryClassification:
        if classification.kind != "exact":
            return classification
        subject = re.split(
            r"\b(?:для|под|к)\b", item_name, maxsplit=1, flags=re.IGNORECASE
        )[0].strip()
        item_classification = self._classifier.classify(subject, procurement_type)
        if (
            item_classification.kind != "exact"
            or item_classification.category_code != classification.category_code
        ):
            return CategoryClassification("none")
        return classification

    def validate_provider_payload(
        self,
        *,
        procurement_type: Literal["goods", "service"],
        source_text: str,
        deterministic: CategoryClassification,
        payload: CategoryClassificationPayload,
    ) -> CategoryResolution:
        codes = _payload_codes(payload)
        expected_prefix = "G" if procurement_type == "goods" else "S"
        if any(
            code not in CATEGORY_NAMES or not code.startswith(expected_prefix)
            for code in codes
        ):
            return CategoryResolution("unresolved", reason_code="invalid_category_code")
        if payload.decision != "unresolved" and not _evidence_supported(
            source_text, payload.evidence
        ):
            return CategoryResolution("unresolved", reason_code="invalid_evidence")
        if deterministic.kind == "exact" and deterministic.category_code:
            if codes != (deterministic.category_code,):
                return CategoryResolution(
                    "unresolved", reason_code="deterministic_conflict"
                )
            return CategoryResolution(
                "deterministic_exact",
                category_code=deterministic.category_code,
                candidate_source="classifier_exact",
            )
        if payload.decision == "exact":
            return CategoryResolution(
                "llm_exact",
                candidates=codes,
                candidate_source="llm_exact",
                requires_confirmation=True,
            )
        if payload.decision == "candidates":
            return CategoryResolution(
                "llm_candidates",
                candidates=codes,
                candidate_source="llm_candidates",
                requires_confirmation=True,
            )
        return CategoryResolution("unresolved", reason_code="provider_unresolved")


def category_taxonomy(
    procurement_type: Literal["goods", "service"],
) -> tuple[CategoryTaxonomyItem, ...]:
    prefix = "G" if procurement_type == "goods" else "S"
    return tuple(
        CategoryTaxonomyItem(code, name, CATEGORY_DESCRIPTIONS[code])
        for code, name in CATEGORY_NAMES.items()
        if code.startswith(prefix)
    )


def category_subject_fingerprint(procurement_type: str, item_name: str) -> str:
    normalized = _normalize(item_name)
    return sha256(
        f"{procurement_type}:{normalized}".encode()
    ).hexdigest()[:16]


def category_classification_strict_json_schema() -> dict:
    return to_strict_json_schema(CategoryClassificationPayload)


def validate_category_classification_schema(schema: dict | None = None) -> list[str]:
    candidate = schema or category_classification_strict_json_schema()
    errors: list[str] = []
    if candidate.get("type") != "object":
        errors.append("root type must be object")
    _validate_schema_node(candidate, "$", errors)
    return errors


def _validate_schema_node(node: object, path: str, errors: list[str]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _validate_schema_node(item, f"{path}[{index}]", errors)
        return
    if not isinstance(node, dict):
        return
    if "default" in node:
        errors.append(f"{path}: default is not allowed")
    if node.get("type") == "object":
        if node.get("additionalProperties") is not False:
            errors.append(f"{path}: object must be closed")
        properties = node.get("properties", {})
        if isinstance(properties, dict) and set(properties) != set(
            node.get("required", [])
        ):
            errors.append(f"{path}: every property must be required")
    for key, value in node.items():
        _validate_schema_node(value, f"{path}.{key}", errors)


def _payload_codes(payload: CategoryClassificationPayload) -> tuple[str, ...]:
    if payload.primary_category_code is not None:
        return (payload.primary_category_code.value,)
    return tuple(item.value for item in payload.alternatives)


def _evidence_supported(source_text: str, evidence: str | None) -> bool:
    if not evidence:
        return False
    return evidence_supports_field("category", source_text, evidence)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-zа-я0-9]+", value.casefold().replace("ё", "е")))
