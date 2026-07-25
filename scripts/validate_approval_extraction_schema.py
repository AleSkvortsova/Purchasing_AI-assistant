import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.extraction.openai_schema import (  # noqa: E402
    approval_extraction_strict_json_schema,
    validate_approval_extraction_schema,
)


def main() -> int:
    schema = approval_extraction_strict_json_schema()
    errors = validate_approval_extraction_schema(schema)
    property_count = len(schema.get("properties", {}))
    print(f"properties: {property_count}")
    if errors:
        print("status: incompatible")
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("status: compatible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
