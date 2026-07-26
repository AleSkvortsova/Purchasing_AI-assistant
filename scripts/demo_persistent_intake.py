import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.intake.models import IntakeFieldUpdate  # noqa: E402
from app.intake_persistence.repositories import (  # noqa: E402
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import (  # noqa: E402
    PersistentIntakeOrchestrator,
)

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _orchestrator(storage: InMemoryIntakeStorage) -> PersistentIntakeOrchestrator:
    return PersistentIntakeOrchestrator(InMemoryIntakePersistenceRepository(storage))


def main() -> int:
    storage = InMemoryIntakeStorage()
    steps = [
        (
            "demo-001",
            IntakeFieldUpdate(
                values={
                    "procurement_type": "goods",
                    "category_code": "G03",
                    "item_name": "Монитор",
                }
            ),
        ),
        (
            "demo-002",
            IntakeFieldUpdate(
                values={
                    "quantity": "10",
                    "unit": "шт.",
                    "specifications": "Диагональ 27 дюймов, IPS",
                    "analogs_allowed": True,
                }
            ),
        ),
        (
            "demo-003",
            IntakeFieldUpdate(values={"amount": "180000", "budget_status": "budgeted"}),
        ),
    ]
    result = None
    for number, (key, update) in enumerate(steps, start=1):
        result = _orchestrator(storage).process_structured_step(
            USER_ID, update, idempotency_key=key
        )
        question = (
            result.intake_result.next_question.field_code
            if result.intake_result.next_question
            else "-"
        )
        print(
            f"Шаг {number}: request={result.request_id} "
            f"version={result.request_version} status={result.intake_result.status} "
            f"next={question}"
        )

    assert result is not None
    replay = _orchestrator(storage).process_structured_step(
        USER_ID, steps[-1][1], idempotency_key=steps[-1][0]
    )
    print(
        f"Replay: replayed={str(replay.replayed).lower()} "
        f"version={replay.request_version}"
    )

    corrected = _orchestrator(storage).process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": "200000"}, explicit_correction=True),
        idempotency_key="demo-004",
    )
    print(
        f"Коррекция: version={corrected.request_version} "
        f"amount={corrected.intake_result.draft.amount}"
    )

    final_updates = [
        IntakeFieldUpdate(
            values={
                "desired_delivery_date": (
                    date.today() + timedelta(days=30)
                ).isoformat(),
                "delivery_location": "Центральный офис",
            }
        ),
        IntakeFieldUpdate(values={"business_justification": "Оснащение рабочих мест"}),
        IntakeFieldUpdate(
            values={"department": "ИТ", "contact_person": "Анна Петрова"},
            source="system",
        ),
    ]
    for offset, update in enumerate(final_updates, start=5):
        result = _orchestrator(storage).process_structured_step(
            USER_ID,
            update,
            idempotency_key=f"demo-{offset:03d}",
        )
        print(
            f"Шаг {offset}: version={result.request_version} "
            f"status={result.intake_result.status}"
        )

    restored = _orchestrator(storage).get_active_session(USER_ID)
    logs = InMemoryIntakePersistenceRepository(storage).list_message_logs(USER_ID)
    print(f"Восстановлено: request={restored.request_id}")
    print(f"Итог: {restored.intake_result.status}")
    print(f"Message logs: {len(logs)}")
    if restored.intake_result.request_card:
        print(f"Карточка: {restored.intake_result.request_card.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
