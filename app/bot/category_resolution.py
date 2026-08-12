import json
import re
from dataclasses import dataclass
from enum import Enum
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
                "candidates for 2-3 plausible categories; otherwise unresolved. "
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
    ) -> CategoryResolution:
        combined_text = " ".join(
            value for value in (item_name, source_text) if value
        )
        deterministic = self._classifier.classify(
            combined_text,
            procurement_type,
        )
        deterministic = self._demote_relational_object_match(
            deterministic,
            procurement_type,
            item_name,
        )
        fingerprint = category_subject_fingerprint(procurement_type, item_name)
        if deterministic.kind == "exact" and deterministic.category_code:
            return CategoryResolution(
                "deterministic_exact",
                category_code=deterministic.category_code,
                candidate_source="classifier_exact",
                subject_fingerprint=fingerprint,
            )
        if deterministic.candidates:
            return CategoryResolution(
                "deterministic_candidates",
                candidates=deterministic.candidates[:4],
                candidate_source="classifier_multiple",
                requires_confirmation=True,
                subject_fingerprint=fingerprint,
            )
        if self._provider is None:
            return CategoryResolution(
                "unresolved",
                reason_code="provider_unavailable",
                subject_fingerprint=fingerprint,
            )
        request = CategoryClassificationRequest(
            procurement_type=procurement_type,
            item_name=item_name,
            source_text=source_text,
            taxonomy_version=CATEGORY_TAXONOMY_VERSION,
            taxonomy=category_taxonomy(procurement_type),
        )
        try:
            payload = self._provider.classify(request)
        except Exception:
            return CategoryResolution(
                "unresolved",
                provider_called=True,
                provider_failed=True,
                reason_code="provider_failure",
                subject_fingerprint=fingerprint,
            )
        try:
            validated = self.validate_provider_payload(
                procurement_type=procurement_type,
                source_text=source_text,
                deterministic=deterministic,
                payload=payload,
            )
        except (AttributeError, TypeError, ValueError):
            return CategoryResolution(
                "unresolved",
                provider_called=True,
                reason_code="malformed_provider_result",
                subject_fingerprint=fingerprint,
            )
        return CategoryResolution(
            **{
                **validated.__dict__,
                "provider_called": True,
                "subject_fingerprint": fingerprint,
            }
        )

    def _demote_relational_object_match(
        self,
        classification: CategoryClassification,
        procurement_type: Literal["goods", "service"],
        item_name: str,
    ) -> CategoryClassification:
        if classification.kind != "exact":
            return classification
        head = re.split(r"\b(?:для|под|к)\b", item_name, maxsplit=1, flags=re.I)[0]
        if head.strip() == item_name.strip() or not head.strip():
            return classification
        head_classification = self._classifier.classify(head, procurement_type)
        if head_classification.kind == "none":
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
    import hashlib

    normalized = _normalize(item_name)
    return hashlib.sha256(
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
