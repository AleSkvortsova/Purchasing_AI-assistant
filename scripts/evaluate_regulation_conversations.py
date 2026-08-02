import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.bot.adapter import TelegramIntakeAdapter  # noqa: E402
from app.bot.dialog_modes import (  # noqa: E402
    InMemoryDialogModeRepository,
    InMemoryDialogModeStorage,
)
from app.bot.keyboards import MENU_INSTRUCTION, MENU_REGULATIONS  # noqa: E402
from app.intake_persistence.exceptions import ActiveDraftNotFoundError  # noqa: E402
from app.rag.answering import (  # noqa: E402
    FakeGroundedAnswerProvider,
    GroundedAnswerPayload,
    RegulationQuestionAnsweringService,
)
from scripts.evaluate_retrieval import build_offline_service  # noqa: E402

DEFAULT_CASES = (
    PROJECT_ROOT
    / "data"
    / "evaluation"
    / "regulation_qa_conversation_cases.json"
)
USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class _NoActiveIntake:
    def get_active_session(self, user_id):
        del user_id
        raise ActiveDraftNotFoundError("not found")

    def process_structured_step(self, *args, **kwargs):
        raise AssertionError("intake must not run in regulation mode")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Telegram adapter evaluation for regulation conversations"
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--show-failures", action="store_true")
    return parser


def evaluate_cases(
    cases: list[dict[str, Any]],
) -> tuple[dict[str, float | int], list[dict[str, Any]], list[dict[str, Any]]]:
    provider = FakeGroundedAnswerProvider(
        GroundedAnswerPayload(
            answer="",
            claims=[],
            insufficient_context=True,
            source_conflict=False,
        )
    )
    qa = RegulationQuestionAnsweringService(build_offline_service(), provider)
    rows = []
    failures = []
    successful_cases = 0
    successful_turns = 0
    expected_turns = 0
    for case_index, case in enumerate(cases, start=1):
        storage = InMemoryDialogModeStorage()
        modes = InMemoryDialogModeRepository(storage)
        adapter = TelegramIntakeAdapter(
            _NoActiveIntake(),
            dialog_modes=modes,
            regulation_qa=qa,
        )
        adapter.handle_menu(USER_ID, MENU_REGULATIONS)
        message_id = case_index * 100
        last_result = None
        last_answer = ""
        case_errors = []
        for turn in case["turns"]:
            role = turn.get("role")
            if role == "user":
                message_id += 1
                outcome = adapter.handle_text(
                    USER_ID,
                    1001,
                    message_id,
                    turn["text"],
                )
                last_answer = outcome.text
                key = f"telegram:1001:{message_id}"
                last_result = storage.replays[(USER_ID, key)][1]
                continue
            if role == "menu":
                adapter.handle_menu(USER_ID, MENU_INSTRUCTION)
                last_result = None
                last_answer = ""
                continue
            if role == "clock":
                pending = storage.pending_regulation.get(USER_ID)
                if pending is not None:
                    storage.pending_regulation[USER_ID] = pending.model_copy(
                        update={
                            "created_at": pending.created_at
                            - timedelta(minutes=turn["advance_minutes"])
                        }
                    )
                continue

            expected_turns += 1
            actual_status = (
                last_result.status
                if last_result is not None
                else modes.get_mode(USER_ID)
            )
            errors = _expectation_errors(
                turn,
                actual_status,
                last_result,
                last_answer,
                storage,
            )
            successful_turns += int(not errors)
            case_errors.extend(errors)
        success = not case_errors
        successful_cases += int(success)
        final_sources = (
            [source.document_id for source in last_result.sources]
            if last_result is not None
            else []
        )
        rows.append(
            {
                "case_id": case["case_id"],
                "status": (
                    last_result.status
                    if last_result is not None
                    else modes.get_mode(USER_ID)
                ),
                "sources": final_sources,
                "success": success,
            }
        )
        if case_errors:
            failures.append(
                {"case_id": case["case_id"], "errors": case_errors}
            )
    return (
        {
            "cases": len(cases),
            "case_success_rate": successful_cases / max(1, len(cases)),
            "turn_success_rate": successful_turns / max(1, expected_turns),
            "failures": len(failures),
        },
        rows,
        failures,
    )


def _expectation_errors(
    expected: dict[str, Any],
    actual_status: str,
    result,
    answer: str,
    storage: InMemoryDialogModeStorage,
) -> list[str]:
    errors = []
    if actual_status != expected["expected_status"]:
        errors.append(
            f"status: expected {expected['expected_status']}, got {actual_status}"
        )
    pending = storage.pending_regulation.get(USER_ID)
    slots = dict(result.diagnostics.get("conversation_slots", {})) if result else {}
    if pending is not None:
        slots.update(
            {
                name: str(value) if name == "amount" else value
                for name, value in pending.known_slots.model_dump().items()
                if value is not None
            }
        )
    for name, value in expected.get("expected_slots", {}).items():
        if slots.get(name) != value:
            errors.append(f"slot {name}: expected {value}, got {slots.get(name)}")
    for name in expected.get("forbidden_slots", []):
        if name in slots:
            errors.append(f"forbidden slot present: {name}")
    if "expected_missing_slots" in expected:
        actual_missing = list(pending.missing_slots) if pending else []
        if actual_missing != expected["expected_missing_slots"]:
            errors.append(
                f"missing slots: expected {expected['expected_missing_slots']}, "
                f"got {actual_missing}"
            )
    if "expected_pending" in expected:
        actual_pending = pending is not None
        if actual_pending != expected["expected_pending"]:
            errors.append(
                f"pending: expected {expected['expected_pending']}, "
                f"got {actual_pending}"
            )
    if "expected_intent" in expected:
        actual_intent = (
            result.diagnostics.get("conversation_primary_intent")
            if result is not None
            else None
        )
        if actual_intent is None and pending is not None:
            actual_intent = pending.primary_intent
        if actual_intent != expected["expected_intent"]:
            errors.append(
                f"intent: expected {expected['expected_intent']}, "
                f"got {actual_intent}"
            )
    if "expected_sources" in expected:
        actual_sources = (
            [source.document_id for source in result.sources]
            if result is not None
            else []
        )
        if actual_sources != expected["expected_sources"]:
            errors.append(
                f"sources: expected {expected['expected_sources']}, "
                f"got {actual_sources}"
            )
    if "expected_reason_code" in expected:
        actual_reason = result.refusal_reason if result is not None else None
        if actual_reason != expected["expected_reason_code"]:
            errors.append(
                f"reason code: expected {expected['expected_reason_code']}, "
                f"got {actual_reason}"
            )
    normalized_answer = answer.casefold()
    for term in expected.get("required_answer_terms", []):
        if term.casefold() not in normalized_answer:
            errors.append(f"answer term missing: {term}")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    metrics, rows, failures = evaluate_cases(cases)
    for name, value in metrics.items():
        rendered = f"{value:.3f}" if isinstance(value, float) else str(value)
        print(f"{name}: {rendered}")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.show_failures and failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
