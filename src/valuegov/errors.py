"""价值贡献治理后端的领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类，携带 HTTP 状态码与稳定错误码。"""

    status = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(DomainError):
    status = 400
    code = "VALIDATION_ERROR"


class ForbiddenError(DomainError):
    status = 403
    code = "FORBIDDEN"


class NotFoundError(DomainError):
    status = 404
    code = "NOT_FOUND"


class ConflictError(DomainError):
    status = 409
    code = "CONFLICT"


class PeriodClosedError(ConflictError):
    code = "PERIOD_CLOSED"
