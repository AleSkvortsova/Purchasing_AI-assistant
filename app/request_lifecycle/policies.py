from app.intake.models import IntakeStatus, IntakeStepResult
from app.schemas.common import RequestStatus


def confirmation_blocking_reasons(
    request_status: RequestStatus,
    result: IntakeStepResult,
    persisted_intake_status: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    if request_status != RequestStatus.DRAFT:
        reasons.append("Заявка уже не является черновиком")
    if result.status != IntakeStatus.READY_FOR_CONFIRMATION:
        reasons.append("Сбор данных заявки не завершён")
    if persisted_intake_status != IntakeStatus.READY_FOR_CONFIRMATION.value:
        reasons.append("Заявка не ожидает подтверждения")
    if not result.completeness.is_complete:
        reasons.append("Не заполнены обязательные поля")
    if result.completeness.invalid_fields:
        reasons.append("Есть поля с некорректными значениями")
    if result.conflicts:
        reasons.append("Есть неразрешённые противоречия")
    if result.request_card is None:
        reasons.append("Итоговая карточка не сформирована")
    if result.approval_context is None:
        reasons.append("Недостаточно данных для контекста согласования")
    if result.approval_route is None:
        reasons.append("Маршрут согласования не рассчитан")
    elif result.approval_route.status != "resolved":
        reasons.append("Маршрут согласования не разрешён")
    return list(dict.fromkeys(reasons))
