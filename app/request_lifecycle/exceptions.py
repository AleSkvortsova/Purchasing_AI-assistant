class RequestLifecycleError(Exception):
    """Base safe lifecycle error."""


class LifecycleRequestNotFoundError(RequestLifecycleError):
    pass


class LifecycleOwnershipError(RequestLifecycleError):
    pass


class LifecycleConcurrentUpdateError(RequestLifecycleError):
    pass


class RequestNotReadyError(RequestLifecycleError):
    def __init__(self, message: str, confirmation_view=None) -> None:
        self.confirmation_view = confirmation_view
        super().__init__(message)


class RequestAlreadyRegisteredError(RequestLifecycleError):
    pass


class RequestAlreadyCancelledError(RequestLifecycleError):
    pass


class LifecycleTransitionError(RequestLifecycleError):
    pass


class LifecycleIdempotencyConflictError(RequestLifecycleError):
    pass


class LifecyclePersistenceError(RequestLifecycleError):
    pass
