import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.intake.models import IntakeFieldUpdate, RequestDraftData  # noqa: E402
from app.intake.service import RequestIntakeService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate one intake step offline")
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--update", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-card", action="store_true")
    args = parser.parse_args()
    try:
        draft = RequestDraftData.model_validate_json(
            args.draft.read_text(encoding="utf-8")
        )
        update = IntakeFieldUpdate.model_validate_json(
            args.update.read_text(encoding="utf-8")
        )
        result = RequestIntakeService().process_step(draft, update)
    except (OSError, ValidationError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"status: {result.status}")
        if result.next_question:
            print(f"next_question: {result.next_question.text}")
        if args.show_card and result.request_card:
            print(result.request_card.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
