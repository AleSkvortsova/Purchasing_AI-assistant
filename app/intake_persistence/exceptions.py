class IntakePersistenceError(Exception):
    """Base safe persistence orchestration error."""


class ActiveDraftNotFoundError(IntakePersistenceError):
    pass


class MultipleActiveDraftsError(IntakePersistenceError):
    def __init__(self, request_ids: list[str]) -> None:
        self.request_ids = request_ids
        super().__init__("У пользователя найдено несколько активных черновиков")


class RequestOwnershipError(IntakePersistenceError):
    pass


class RequestNotEditableError(IntakePersistenceError):
    pass


class DialogStateCorruptedError(IntakePersistenceError):
    pass


class UnsupportedIntakeSchemaVersionError(IntakePersistenceError):
    pass


class ConcurrentIntakeUpdateError(IntakePersistenceError):
    pass


class IdempotencyConflictError(IntakePersistenceError):
    pass


class PersistencePartialFailureError(IntakePersistenceError):
    pass


class IntakePersistenceMappingError(IntakePersistenceError):
    pass


class IntakePersistenceRepositoryError(IntakePersistenceError):
    pass
