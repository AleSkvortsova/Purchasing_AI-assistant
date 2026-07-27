import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.intake.models import IntakeFieldUpdate  # noqa: E402
from app.intake.service import RequestIntakeService  # noqa: E402
from app.intake_persistence.repositories import (  # noqa: E402
    InMemoryIntakePersistenceRepository,
    InMemoryIntakeStorage,
)
from app.intake_persistence.service import PersistentIntakeOrchestrator  # noqa: E402
from app.request_lifecycle.exceptions import (  # noqa: E402
    RequestAlreadyRegisteredError,
)
from app.request_lifecycle.repositories import (  # noqa: E402
    InMemoryRequestLifecycleRepository,
)
from app.request_lifecycle.service import RequestLifecycleService  # noqa: E402
from app.rules.repository import InMemoryApprovalRuleRepository  # noqa: E402
from app.rules.service import ApprovalRuleService  # noqa: E402
from scripts.validate_approval_rules import load_rule_seed  # noqa: E402

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _core() -> RequestIntakeService:
    _, base, additional = load_rule_seed()
    return RequestIntakeService(
        ApprovalRuleService(InMemoryApprovalRuleRepository(base, additional))
    )


def _services(storage):
    core = _core()
    intake = PersistentIntakeOrchestrator(
        InMemoryIntakePersistenceRepository(storage), core
    )
    lifecycle = RequestLifecycleService(
        InMemoryRequestLifecycleRepository(storage), core
    )
    return intake, lifecycle


def _ready(intake, key: str):
    return intake.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(
            values={
                "procurement_type": "goods",
                "category_code": "G03",
                "item_name": "Монитор",
                "quantity": "10",
                "unit": "шт.",
                "specifications": "27 дюймов",
                "analogs_allowed": True,
                "amount": "180000",
                "budget_status": "budgeted",
                "desired_delivery_date": (
                    date.today() + timedelta(days=30)
                ).isoformat(),
                "delivery_location": "Офис",
                "business_justification": "Оснащение рабочих мест",
                "department": "ИТ",
                "contact_person": "Анна Петрова",
            }
        ),
        idempotency_key=key,
    )


def _lifecycle_log_count(storage) -> int:
    return sum(bool(log.metadata.get("lifecycle")) for log in storage.message_logs)


def main() -> int:
    storage_a = InMemoryIntakeStorage()
    intake_a, lifecycle_a = _services(storage_a)
    ready_a = _ready(intake_a, "a-ready")
    view = lifecycle_a.get_confirmation_view(ready_a.request_id, USER_ID)
    registered = lifecycle_a.confirm_request(
        ready_a.request_id, USER_ID, ready_a.request_version, "a-confirm"
    )
    replay = lifecycle_a.confirm_request(ready_a.request_id, USER_ID, 1, "a-confirm")
    try:
        lifecycle_a.confirm_request(
            ready_a.request_id, USER_ID, registered.version, "a-confirm-new"
        )
    except RequestAlreadyRegisteredError:
        already_registered = True
    else:
        already_registered = False
    print(
        "A confirm:",
        f"request_id={registered.request_id}",
        f"version={registered.version}",
        f"status={registered.status}",
        f"intake={registered.intake_status}",
        f"number={registered.request_number}",
        f"confirmable={str(view.confirmable).lower()}",
        f"replay={str(replay.replayed).lower()}",
        f"already_registered={str(already_registered).lower()}",
        f"logs={_lifecycle_log_count(storage_a)}",
    )

    storage_b = InMemoryIntakeStorage()
    intake_b, lifecycle_b = _services(storage_b)
    ready_b = _ready(intake_b, "b-ready")
    editing = lifecycle_b.return_to_editing(
        ready_b.request_id, USER_ID, ready_b.request_version, "b-edit"
    )
    corrected = intake_b.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"amount": "220000"}, explicit_correction=True),
        request_id=ready_b.request_id,
        idempotency_key="b-correction",
    )
    registered_b = lifecycle_b.confirm_request(
        ready_b.request_id, USER_ID, corrected.request_version, "b-confirm"
    )
    print(
        "B edit:",
        f"request_id={registered_b.request_id}",
        f"version={registered_b.version}",
        f"status={registered_b.status}",
        f"from={editing.intake_status}",
        f"number={registered_b.request_number}",
        "amount=220000",
        f"logs={_lifecycle_log_count(storage_b)}",
    )

    storage_c = InMemoryIntakeStorage()
    intake_c, lifecycle_c = _services(storage_c)
    draft_c = intake_c.process_structured_step(
        USER_ID,
        IntakeFieldUpdate(values={"item_name": "Отменяемая потребность"}),
    )
    cancelled = lifecycle_c.cancel_draft(
        draft_c.request_id,
        USER_ID,
        draft_c.request_version,
        "c-cancel",
        "Потребность больше не актуальна",
    )
    next_draft = intake_c.process_structured_step(
        USER_ID, IntakeFieldUpdate(values={"item_name": "Новая потребность"})
    )
    print(
        "C cancel:",
        f"request_id={cancelled.request_id}",
        f"version={cancelled.version}",
        f"status={cancelled.status}",
        f"intake={cancelled.intake_status}",
        f"number={cancelled.request_number}",
        f"new_request={next_draft.request_id}",
        f"logs={_lifecycle_log_count(storage_c)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
