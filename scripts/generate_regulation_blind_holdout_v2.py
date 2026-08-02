import json
import random
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = 20260802


def generate_cases() -> list[dict[str, Any]]:
    rng = random.Random(SEED)

    def choose(*variants: str) -> str:
        return rng.choice(variants)

    cases = [
        _case(
            "blind-v2-01-budgeted-approval",
            choose(
                "На согласование отправляем закупку на 275 тысяч, расходы "
                "учтены. Кому она отправится на согласование?",
                "Расходы учтены, стоимость покупки 275 тысяч. Кто согласует?",
            ),
            ["approval_route"],
            {"amount": "275000", "budget_status": "budgeted"},
            "answered",
            ["kb-009"],
            ["финансовый контролёр"],
        ),
        _case(
            "blind-v2-02-unbudgeted-approval",
            "Покупка на 130 000 рублей, расходы не учтены. Кто согласует?",
            ["approval_route"],
            {"amount": "130000", "budget_status": "unbudgeted"},
            "answered",
            ["kb-009"],
            ["финансовый директор", "генеральный директор"],
        ),
        _case(
            "blind-v2-03-uncertain-budget",
            "Заявка на 75 тыс.; возможно, сумма есть в плане. "
            "Какие согласования потребуются?",
            ["approval_route"],
            {"amount": "75000", "budget_status": "unknown"},
            "clarification_required",
            [],
            [],
            clarification=r"предусмотрена ли.*бюджет",
        ),
        _case(
            "blind-v2-04-status-reason",
            "Почему у заявки установлен статус «На согласовании»?",
            ["status_explanation"],
            {"status_name": "на согласовании"},
            "answered",
            ["kb-007"],
            ["На согласовании"],
        ),
        _case(
            "blind-v2-05-forum-urgent",
            "Форум состоится через 18 дней. Это высокий приоритет?",
            ["urgency_policy"],
            {
                "duration_days": 18,
                "relative_deadline": "in_days",
                "category_hint": "S07",
            },
            "answered",
            ["kb-006"],
            ["18", "30", "P2"],
        ),
        _case(
            "blind-v2-06-exhibition-regular",
            "Выставка запланирована через 32 дня. Заявка уже срочная?",
            ["urgency_policy"],
            {
                "duration_days": 32,
                "relative_deadline": "in_days",
                "category_hint": "S07",
            },
            "answered",
            ["kb-006"],
            ["32", "30"],
            forbidden=["основание для предварительного приоритета P2"],
        ),
        _case(
            "blind-v2-07-presentation-urgent-fields",
            "Презентация нужна через 12 дней. Будет ли заявка срочной и "
            "что дополнительно написать?",
            ["urgency_policy", "required_fields"],
            {
                "duration_days": 12,
                "relative_deadline": "in_days",
                "category_hint": "S07",
            },
            "answered",
            ["kb-006"],
            ["P2", "причина срочности", "подтверждение руководителя"],
        ),
        _case(
            "blind-v2-08-transferred-status",
            "Что происходит после статуса «Передана в отдел закупок»?",
            ["status_explanation"],
            {"status_name": "transferred_to_procurement"},
            "answered",
            ["kb-007", "kb-001"],
            ["проверяет полноту"],
        ),
        _case(
            "blind-v2-09-cancel-before-work",
            "Можно отменить заявку, пока она ещё не принята в работу?",
            ["request_cancellation"],
            {},
            "answered",
            ["kb-001"],
            ["до статуса «Принята в работу»"],
        ),
        _case(
            "blind-v2-10-transport-fields",
            "Что заполнить для перевозки шестнадцати ящиков между филиалами?",
            ["category_classification", "required_fields"],
            {"category_hint": "S03"},
            "answered",
            ["kb-005"],
            ["маршрут", "вес", "объём"],
        ),
        _case(
            "blind-v2-11-integration-fields",
            "Нужно настроить обмен бухгалтерской платформы с витриной. "
            "К какой категории относится услуга и какие сведения нужны?",
            ["category_classification", "required_fields"],
            {"category_hint": "S05", "purchase_type": "service"},
            "answered",
            ["kb-005"],
            ["S05", "бизнес-требования", "приёмка"],
        ),
        _case(
            "blind-v2-12-mixed-request",
            "Разрешено ли в одной заявке объединить оборудование и услугу "
            "его монтажа?",
            ["category_classification", "required_fields"],
            {},
            "answered",
            ["kb-015"],
            ["отдельные заявки"],
        ),
        _case(
            "blind-v2-13-brand-policy",
            "Можно указать марку оборудования и не допускать аналоги?",
            ["brand_equivalent_policy"],
            {"purchase_type": "goods"},
            "answered",
            ["kb-010", "kb-001"],
            ["эквивалент", "обосновать"],
        ),
        _case(
            "blind-v2-14-responsibility",
            "Кто должен подготовить техническое описание закупаемого товара?",
            ["responsibility_policy"],
            {"purchase_type": "goods"},
            "answered",
            ["kb-001", "kb-010"],
            ["внутренний заказчик"],
        ),
        _case(
            "blind-v2-15-partial-draft",
            "Я заполню часть заявки сейчас, а остальные сведения добавлю позже. "
            "Так можно?",
            ["draft_and_history"],
            {},
            "answered",
            ["kb-010", "kb-014"],
            ["черновик"],
        ),
        _case(
            "blind-v2-16-past-requests",
            "Где найти заявки, которые я подавала раньше?",
            ["draft_and_history"],
            {},
            "answered",
            ["kb-014"],
            ["Мои заявки"],
        ),
        _case(
            "blind-v2-17-supplier-advice",
            "Кого из поставщиков вы рекомендуете для офисной техники?",
            ["supplier_recommendation"],
            {},
            "insufficient_context",
            [],
            [],
            outside=True,
        ),
        _case(
            "blind-v2-18-contractor-conditions",
            "Какой подрядчик сейчас предлагает лучшие условия для ремонта?",
            ["supplier_recommendation"],
            {},
            "insufficient_context",
            [],
            [],
            outside=True,
        ),
        _case(
            "blind-v2-19-approval-clarification",
            "Чьё одобрение понадобится?",
            ["approval_route"],
            {},
            "clarification_required",
            [],
            [],
            clarification=r"сумм.*бюджет",
        ),
        _case(
            "blind-v2-20-fields-clarification",
            "Что потребуется указать для закупки?",
            ["required_fields"],
            {},
            "clarification_required",
            [],
            [],
            clarification=r"товар или услугу.*предмет закупки",
        ),
    ]
    _assert_new_questions(cases)
    return cases


def _case(
    case_id: str,
    question: str,
    intents: list[str],
    slots: dict[str, Any],
    status: str,
    documents: list[str],
    required: list[str],
    *,
    forbidden: list[str] | None = None,
    clarification: str | None = None,
    outside: bool = False,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "question": question,
        "expected_intents": intents,
        "expected_slots": slots,
        "expected_status": status,
        "expected_document_ids": documents,
        "required_claims": required,
        "forbidden_claims": forbidden or [],
        "clarification_text_pattern": clarification,
        "outside_kb": outside,
    }


def _assert_new_questions(cases: list[dict[str, Any]]) -> None:
    known_questions: set[str] = set()
    for filename in (
        "regulation_qa_production_cases.json",
        "regulation_qa_holdout_cases.json",
        "regulation_qa_blind_holdout_cases.json",
    ):
        path = PROJECT_ROOT / "data" / "evaluation" / filename
        known_questions.update(
            item["question"]
            for item in json.loads(path.read_text(encoding="utf-8"))
        )
    questions = [case["question"] for case in cases]
    if len(known_questions) != 65:
        raise ValueError("Expected exactly 65 previous evaluation questions")
    if len(cases) != 20 or len(set(questions)) != 20:
        raise ValueError("Second blind holdout must contain 20 unique questions")
    if known_questions.intersection(questions):
        raise ValueError("Second blind holdout contains a previous question")


def main() -> int:
    print(json.dumps(generate_cases(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
