import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.intake.models import (  # noqa: E402
    IntakeFieldUpdate,
    RequestDraftData,
)
from app.intake.service import RequestIntakeService  # noqa: E402


def _run_compact_demo() -> None:
    service = RequestIntakeService()
    draft = RequestDraftData()
    steps = [
        {
            "procurement_type": "goods",
            "category_code": "G03",
            "item_name": "Монитор",
        },
        {
            "quantity": "10",
            "unit": "шт.",
            "specifications": "Диагональ 27 дюймов",
            "analogs_allowed": True,
        },
        {"amount": "180000", "budget_status": "budgeted"},
        {
            "desired_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
            "delivery_location": "Центральный офис",
        },
        {
            "business_justification": "Оснащение рабочих мест",
            "department": "ИТ",
            "contact_person": "Анна Петрова",
        },
    ]
    for number, values in enumerate(steps, start=1):
        result = service.process_step(draft, IntakeFieldUpdate(values=values))
        draft = result.draft
        question = (
            result.next_question.text if result.next_question else "карточка готова"
        )
        print(f"Шаг {number}: {result.status}; {question}")


def _run_from_empty_demo() -> None:
    service = RequestIntakeService()
    draft = RequestDraftData()
    profile = IntakeFieldUpdate(
        values={"department": "ИТ", "contact_person": "Анна Петрова"},
        source="system",
    )
    draft = service.process_step(draft, profile).draft
    answers = {
        "procurement_type": {"procurement_type": "goods"},
        "item_name": {
            "item_name": "Монитор",
            "preferred_brand": "Samsung",
        },
        "category_code": {"category_code": "G03"},
        "quantity": {"quantity": "10"},
        "unit": {"unit": "шт."},
        "specifications": {"specifications": "Диагональ 27 дюймов, IPS"},
        "analogs_allowed": {"analogs_allowed": False},
        "brand_justification": {
            "brand_justification": "Совместимость с рабочими местами"
        },
        "amount": {"amount": "180000"},
        "budget_status": {"budget_status": "budgeted"},
        "desired_delivery_date": {
            "desired_delivery_date": (date.today() + timedelta(days=30)).isoformat()
        },
        "delivery_location": {"delivery_location": "Центральный офис"},
        "business_justification": {
            "business_justification": "Оснащение новых рабочих мест"
        },
    }
    result = service.process_step(draft, IntakeFieldUpdate())
    questions = 0
    while result.next_question is not None:
        question = result.next_question
        values = answers.get(question.field_code)
        if values is None:
            raise RuntimeError(f"Нет demo-ответа для поля {question.field_code}")
        questions += 1
        answer = ", ".join(f"{key}={value}" for key, value in values.items())
        print(f"Шаг {questions}: {question.text} => {answer}")
        result = service.process_step(
            result.draft,
            IntakeFieldUpdate(values=values),
        )
    print(f"Итог: {result.status}")
    print(f"Вопросов задано: {questions}")
    print("Источник system: department, contact_person")
    user_fields = [
        code
        for code, state in result.draft.field_states.items()
        if state.source == "user"
    ]
    print(f"Источник user: {', '.join(user_fields)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline intake dialog demo")
    parser.add_argument("--from-empty", action="store_true")
    args = parser.parse_args()
    if args.from_empty:
        _run_from_empty_demo()
    else:
        _run_compact_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
