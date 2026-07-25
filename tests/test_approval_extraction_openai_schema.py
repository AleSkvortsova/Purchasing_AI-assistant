from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from app.extraction.openai_schema import (
    OpenAIApprovalExtractionPayload,
    approval_extraction_strict_json_schema,
    validate_approval_extraction_schema,
)
from scripts import validate_approval_extraction_schema as schema_cli


def _payload_data() -> dict:
    return {
        "amount_raw": "600 тысяч",
        "budget_status_raw": "budgeted",
        "urgency_raw": None,
        "single_supplier_raw": True,
        "category_raw": "S11",
        "has_data_access_raw": None,
        "work_on_site_raw": None,
        "urgency_claimed": False,
        "confidence_items": [
            {"field_name": "amount", "confidence": 0.95},
            {"field_name": "category", "confidence": 0.9},
        ],
        "evidence_items": [
            {"field_name": "amount", "evidence": "600 тысяч"},
            {"field_name": "category", "evidence": "юридические услуги"},
        ],
        "unknown_fields": ["urgency"],
        "contradictions": [],
    }


def _schema_nodes(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _schema_nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _schema_nodes(nested)


def test_generated_schema_matches_strict_subset() -> None:
    schema = approval_extraction_strict_json_schema()

    assert validate_approval_extraction_schema(schema) == []
    assert schema["type"] == "object"
    assert "anyOf" not in schema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])


def test_schema_has_no_defaults_or_open_objects() -> None:
    schema = approval_extraction_strict_json_schema()

    for node in _schema_nodes(schema):
        assert "default" not in node
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert not isinstance(node.get("additionalProperties"), dict)
            assert set(node.get("properties", {})) == set(
                node.get("required", [])
            )


def test_nullable_fields_are_required_and_allow_null() -> None:
    schema = approval_extraction_strict_json_schema()
    nullable_fields = {
        "amount_raw",
        "budget_status_raw",
        "urgency_raw",
        "single_supplier_raw",
        "category_raw",
        "has_data_access_raw",
        "work_on_site_raw",
    }

    for field_name in nullable_fields:
        field_schema = schema["properties"][field_name]
        assert field_name in schema["required"]
        assert {branch.get("type") for branch in field_schema["anyOf"]} >= {
            "null"
        }


def test_dto_converts_to_domain_model() -> None:
    payload = OpenAIApprovalExtractionPayload.model_validate(_payload_data())

    raw = payload.to_raw_extraction()

    assert raw.amount_raw == "600 тысяч"
    assert raw.confidence_by_field == {"amount": 0.95, "category": 0.9}
    assert raw.evidence_by_field == {
        "amount": "600 тысяч",
        "category": "юридические услуги",
    }
    assert raw.unknown_fields == ["urgency"]


def test_duplicate_evidence_field_is_rejected() -> None:
    data = _payload_data()
    data["evidence_items"] = [
        {"field_name": "amount", "evidence": "600 тысяч"},
        {"field_name": "amount", "evidence": "на 600 тысяч"},
    ]

    with pytest.raises(ValidationError, match="duplicate field_name"):
        OpenAIApprovalExtractionPayload.model_validate(data)


def test_duplicate_confidence_field_is_rejected() -> None:
    data = _payload_data()
    data["confidence_items"] = [
        {"field_name": "amount", "confidence": 0.9},
        {"field_name": "amount", "confidence": 0.8},
    ]

    with pytest.raises(ValidationError, match="duplicate field_name"):
        OpenAIApprovalExtractionPayload.model_validate(data)


@pytest.mark.parametrize("collection", ["confidence_items", "evidence_items"])
def test_unknown_field_name_is_rejected(collection: str) -> None:
    data = _payload_data()
    item = dict(data[collection][0])
    item["field_name"] = "unknown"
    data[collection] = [item]

    with pytest.raises(ValidationError):
        OpenAIApprovalExtractionPayload.model_validate(data)


def test_schema_cli_reports_property_count(capsys) -> None:
    assert schema_cli.main() == 0
    assert capsys.readouterr().out == (
        "properties: 12\nstatus: compatible\n"
    )


def test_schema_cli_returns_nonzero_for_incompatible_schema(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        schema_cli,
        "approval_extraction_strict_json_schema",
        lambda: {"type": "array"},
    )

    assert schema_cli.main() == 1
    output = capsys.readouterr().out
    assert "status: incompatible" in output
    assert "root type must be object" in output
