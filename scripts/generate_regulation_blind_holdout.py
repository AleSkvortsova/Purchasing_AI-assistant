import json
import random
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = 20260801


def generate_cases() -> list[dict[str, Any]]:
    rng = random.Random(SEED)

    def choose(*variants: str) -> str:
        return rng.choice(variants)

    cases = [
        _case(
            "blind-01-budgeted-approval",
            choose(
                "Бюджет на покупку подтверждён, сумма 320 тысяч. "
                "Кому уйдёт на согласование?",
                "На закупку заложено 320 тысяч рублей. Кто её согласует?",
            ),
            ["approval_route"],
            {"amount": "320000", "budget_status": "budgeted"},
            "answered",
            ["kb-009"],
            ["финансовый контролёр"],
        ),
        _case(
            "blind-02-unbudgeted-approval",
            choose(
                "Планируется внебюджетная закупка за 85 000 рублей. "
                "Через каких согласующих она пройдёт?",
                "Покупка на 85 тысяч не предусмотрена бюджетом. Кто согласует?",
            ),
            ["approval_route"],
            {"amount": "85000", "budget_status": "unbudgeted"},
            "answered",
            ["kb-009"],
            ["финансовый директор"],
        ),
        _case(
            "blind-03-approval-unknown-budget",
            "Для заявки на 210 тыс. нужен маршрут, но бюджетный статус неизвестен.",
            ["approval_route"],
            {"amount": "210000", "budget_status": "unknown"},
            "clarification_required",
            [],
            [],
            clarification=r"предусмотрена ли.*бюджет",
        ),
        _case(
            "blind-04-approval-missing-amount",
            "Какой маршрут согласования у бюджетной закупки?",
            ["approval_route"],
            {"budget_status": "budgeted"},
            "clarification_required",
            [],
            [],
            clarification=r"сумм.*бюджет",
        ),
        _case(
            "blind-05-event-twenty-days",
            "Конференция состоится через двадцать дней. Это основание для P2?",
            ["urgency_policy"],
            {
                "duration_days": 20,
                "relative_deadline": "in_days",
                "category_hint": "S07",
            },
            "answered",
            ["kb-006"],
            ["20", "30", "P2"],
        ),
        _case(
            "blind-06-event-thirty-five-days",
            "Мероприятие будет через 35 дней. Это срочная заявка?",
            ["urgency_policy"],
            {"duration_days": 35, "relative_deadline": "in_days"},
            "answered",
            ["kb-006"],
            ["35", "30"],
            forbidden=["основание для предварительного приоритета P2"],
        ),
        _case(
            "blind-07-high-priority-policy",
            "По каким признакам заявке назначают высокий приоритет?",
            ["urgency_policy"],
            {},
            "answered",
            ["kb-006"],
            ["P2", "окончательное решение"],
        ),
        _case(
            "blind-08-rework-status",
            "Что означает, когда заявке присвоили статус «Требует доработки»?",
            ["status_explanation"],
            {"status_name": "requires_rework"},
            "answered",
            ["kb-007", "kb-010"],
            ["недостающие сведения"],
        ),
        _case(
            "blind-09-cancel-before-work",
            "Разрешена ли отмена заявки до того, как её приняли в работу?",
            ["request_cancellation"],
            {},
            "answered",
            ["kb-001"],
            ["до статуса «Принята в работу»"],
        ),
        _case(
            "blind-10-transport-fields",
            "Какие параметры нужны, чтобы перевезти восемь контейнеров "
            "с одной площадки на другую?",
            ["category_classification", "required_fields"],
            {"category_hint": "S03"},
            "answered",
            ["kb-005"],
            ["маршрут", "вес", "объём"],
        ),
        _case(
            "blind-11-system-integration",
            "Нужна интеграция кадровой системы с корпоративным порталом. "
            "К какой категории это относится и какие поля заполнить?",
            ["category_classification", "required_fields"],
            {"category_hint": "S05", "purchase_type": "service"},
            "answered",
            ["kb-005"],
            ["S05", "бизнес-требования", "приёмка"],
        ),
        _case(
            "blind-12-mixed-goods-services",
            "Можно ли в одной заявке объединить оборудование и услуги "
            "по его настройке?",
            ["category_classification", "required_fields"],
            {},
            "answered",
            ["kb-015"],
            ["отдельные заявки"],
        ),
        _case(
            "blind-13-brand-equivalent",
            "Хотим указать конкретную марку техники. Как оформить запрет аналогов?",
            ["brand_equivalent_policy"],
            {},
            "answered",
            ["kb-010", "kb-001"],
            ["эквивалент", "обосновать"],
        ),
        _case(
            "blind-14-requirements-owner",
            "Кто отвечает за подготовку характеристик закупаемого товара?",
            ["responsibility_policy"],
            {"purchase_type": "goods"},
            "answered",
            ["kb-001", "kb-010"],
            ["внутренний заказчик"],
        ),
        _case(
            "blind-15-save-draft",
            "Можно сохранить незаполненную заявку и вернуться к ней позднее?",
            ["draft_and_history"],
            {},
            "answered",
            ["kb-010", "kb-014"],
            ["черновик"],
        ),
        _case(
            "blind-16-request-history",
            "В каком разделе находятся мои последние отправленные заявки?",
            ["draft_and_history"],
            {},
            "answered",
            ["kb-014"],
            ["Мои заявки"],
        ),
        _case(
            "blind-17-contractor-recommendation",
            "Какого подрядчика надёжнее выбрать для ремонта офиса?",
            ["supplier_recommendation"],
            {},
            "insufficient_context",
            [],
            [],
            outside=True,
        ),
        _case(
            "blind-18-carrier-recommendation",
            "Какого перевозчика вы рекомендуете для этой доставки?",
            ["supplier_recommendation"],
            {},
            "insufficient_context",
            [],
            [],
            outside=True,
        ),
        _case(
            "blind-19-fields-missing-subject",
            "Какие данные мне заполнить?",
            ["required_fields"],
            {},
            "clarification_required",
            [],
            [],
            clarification=r"товар или услугу.*предмет закупки",
        ),
        _case(
            "blind-20-general-help",
            "Дай краткий обзор правил закупок.",
            ["general_help"],
            {},
            "clarification_required",
            [],
            ["Я могу помочь", "Уточните"],
            clarification=r"Я могу помочь",
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
        "regulation_qa_cases.json",
        "regulation_qa_production_cases.json",
        "regulation_qa_holdout_cases.json",
    ):
        path = PROJECT_ROOT / "data" / "evaluation" / filename
        known_questions.update(
            item["question"]
            for item in json.loads(path.read_text(encoding="utf-8"))
        )
    questions = [case["question"] for case in cases]
    if len(cases) != 20 or len(set(questions)) != 20:
        raise ValueError("Blind holdout must contain 20 unique questions")
    if known_questions.intersection(questions):
        raise ValueError("Blind holdout contains a previously used question")


def main() -> int:
    print(json.dumps(generate_cases(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
