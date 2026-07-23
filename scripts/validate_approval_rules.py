import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.rag.loader import load_documents  # noqa: E402
from app.rules.models import ApprovalRule  # noqa: E402

DEFAULT_RULES_PATH = PROJECT_ROOT / "data" / "rules" / "approval_rules.json"
KNOWLEDGE_BASE = PROJECT_ROOT / "knowledge_base"


def load_rule_seed(
    path: Path = DEFAULT_RULES_PATH,
) -> tuple[dict[str, Any], list[ApprovalRule], list[ApprovalRule]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    base = [
        ApprovalRule.model_validate({**value, "rule_kind": "base"})
        for value in data["base_rules"]
    ]
    additional = [
        ApprovalRule.model_validate({**value, "rule_kind": "additional"})
        for value in data["additional_rules"]
    ]
    return data, base, additional


def validate_approval_rules(
    path: Path = DEFAULT_RULES_PATH,
) -> list[str]:
    errors: list[str] = []
    try:
        data, base, additional = load_rule_seed(path)
    except (OSError, KeyError, json.JSONDecodeError, ValidationError) as exc:
        return [f"Cannot load approval rules: {exc}"]

    if not data.get("schema_version") or not data.get("generated_from"):
        errors.append("schema_version and generated_from are required")

    all_rules = [*base, *additional]
    codes = [rule.rule_code for rule in all_rules]
    duplicates = sorted(
        code for code, count in Counter(codes).items() if count > 1
    )
    if duplicates:
        errors.append("Duplicate rule_code: " + ", ".join(duplicates))

    for status in ("budgeted", "unbudgeted"):
        rules = sorted(
            (
                rule
                for rule in base
                if rule.budget_status == status and rule.is_active
            ),
            key=lambda rule: rule.min_amount or Decimal(0),
        )
        if not rules or rules[0].min_amount != 0:
            errors.append(f"{status}: ranges must start at 0")
            continue
        for previous, current in zip(rules, rules[1:], strict=False):
            if previous.max_amount is None:
                errors.append(
                    f"{status}: open range {previous.rule_code} overlaps "
                    f"{current.rule_code}"
                )
                continue
            expected = previous.max_amount + Decimal("0.01")
            if current.min_amount != expected:
                errors.append(
                    f"{status}: gap or overlap between "
                    f"{previous.rule_code} and {current.rule_code}"
                )
        if rules[-1].max_amount is not None:
            errors.append(f"{status}: final range must be open-ended")

    documents = {
        document.document_id: document
        for document in load_documents(KNOWLEDGE_BASE)
    }
    category_codes = _category_codes(
        documents["kb-004"].content_without_front_matter
    )
    for rule in all_rules:
        document = documents.get(rule.source_document_id)
        if document is None:
            errors.append(
                f"{rule.rule_code}: unknown source_document_id "
                f"{rule.source_document_id}"
            )
        elif rule.source_section not in document.content_without_front_matter:
            errors.append(
                f"{rule.rule_code}: source section not found: "
                f"{rule.source_section}"
            )
        if (
            rule.condition_type == "category"
            and rule.condition_value not in category_codes
        ):
            errors.append(
                f"{rule.rule_code}: unknown category_code "
                f"{rule.condition_value}"
            )
    return errors


def _category_codes(content: str) -> set[str]:
    return set(re.findall(r"^\|\s*([GS]\d{2})\s*\|", content, re.MULTILINE))


def main() -> int:
    errors = validate_approval_rules()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    _, base, additional = load_rule_seed()
    print(f"Base rules: {len(base)}")
    print(f"Additional rules: {len(additional)}")
    print("Approval rules are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
