from enum import StrEnum


class UserRole(StrEnum):
    REQUESTER = "requester"
    BUYER = "buyer"
    ADMIN = "admin"


class RequestType(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"


class RequestStatus(StrEnum):
    DRAFT = "draft"
    NEW = "new"
    CANCELLED = "cancelled"


class DatabaseHealthStatus(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    ERROR = "error"
