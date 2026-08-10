from collections.abc import Callable
from datetime import date

import pytest

import app.intake.validators as intake_validators


class _FrozenDateMeta(type):
    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, date)


@pytest.fixture
def freeze_intake_today(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[date], None]:
    """Freeze only the intake validator's clock for a test scenario."""

    def freeze(today: date) -> None:
        class FrozenDate(date, metaclass=_FrozenDateMeta):
            @classmethod
            def today(cls) -> date:
                return today

        monkeypatch.setattr(intake_validators, "date", FrozenDate)

    return freeze
