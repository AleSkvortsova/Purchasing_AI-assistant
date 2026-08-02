import json
from pathlib import Path

from scripts.evaluate_regulation_conversations import evaluate_cases

CASES = Path("data/evaluation/regulation_qa_conversation_cases.json")


def test_conversation_dataset_has_fifteen_multi_turn_cases() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))

    assert len(cases) >= 15
    assert all(
        sum(turn.get("role") in {"user", "menu"} for turn in case["turns"])
        >= 2
        for case in cases
    )


def test_conversation_dataset_passes_through_telegram_adapter() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))

    metrics, rows, failures = evaluate_cases(cases)

    assert metrics["case_success_rate"] == 1
    assert metrics["turn_success_rate"] == 1
    assert failures == []
    assert all(row["success"] for row in rows)
