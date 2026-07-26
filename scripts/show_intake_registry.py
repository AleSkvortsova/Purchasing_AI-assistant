import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.intake.field_registry import RequestFieldRegistry  # noqa: E402
from app.intake.models import ProcurementType, RequestDraftData  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Show deterministic intake registry")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--type", choices=[item.value for item in ProcurementType])
    parser.add_argument("--category")
    parser.add_argument("--required-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    registry = RequestFieldRegistry()
    draft = RequestDraftData(
        procurement_type=args.type,
        category_code=args.category.upper() if args.category else None,
    )
    applicable_codes = {item.code for item in registry.applicable(draft)}
    has_context = args.type is not None or args.category is not None
    rows = []
    for item in sorted(
        registry.all(), key=lambda field: (field.priority, field.display_order)
    ):
        applicable = item.code in applicable_codes
        required = applicable and registry.is_required(item, draft)
        if has_context and not args.all and not applicable:
            continue
        if args.required_only and not required:
            continue
        rows.append(
            {
                "priority": item.priority,
                "code": item.code,
                "label": item.label,
                "required_scope": item.required_scope,
                "required_when": item.required_when,
                "question": item.question,
                "applicable": applicable,
                "required": required,
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    columns = [
        "priority",
        "code",
        "label",
        "required_scope",
        "required_when",
        "question",
    ]
    if has_context:
        columns.extend(["applicable", "required"])
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(row[column] or "-") for column in columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
