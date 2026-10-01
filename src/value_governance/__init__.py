"""价值贡献治理后端。

统一维护组织边界、产业分类、项目任务、投资批次、研发费用、基础研究
属性、成果转化、公共服务贡献与内部交易抵销；报告期关账即冻结公式、
组织范围与数据快照，历史更正走可审计的更正版本。
"""

from .errors import (
    GovernanceError,
    InvalidStateError,
    NotFoundError,
    PeriodClosedError,
    PermissionDeniedError,
    ValidationError,
)
from .db import open_store
from .engine import FORMULA_VERSION, compute_report
from .service import GovernanceService
from .jobs import resume_jobs, sweep_reminders

__all__ = [
    "GovernanceError",
    "InvalidStateError",
    "NotFoundError",
    "PeriodClosedError",
    "PermissionDeniedError",
    "ValidationError",
    "CloseBlockedError",
    "open_store",
    "GovernanceService",
    "FORMULA_VERSION",
    "compute_report",
    "resume_jobs",
    "sweep_reminders",
]
