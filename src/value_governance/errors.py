"""治理后端统一错误类型。"""

from __future__ import annotations


class GovernanceError(Exception):
    """所有业务规则错误的基类。"""


class ValidationError(GovernanceError):
    """数据不满足可审计规则（如分摊份额合计不为 1）。"""


class PermissionDeniedError(GovernanceError):
    """角色或组织边界不允许该操作，例如提交人自批、单位越权改主数据。"""


class PeriodClosedError(GovernanceError):
    """报告期已关账，公式、组织范围、目标与历史数据均已冻结。"""


class InvalidStateError(GovernanceError):
    """对象当前状态不允许该操作，如对未立项的项目关账。"""


class NotFoundError(GovernanceError):
    """引用的对象不存在。"""


class CloseBlockedError(GovernanceError):
    """关账前置条件未满足，错误中携带全部阻断项，便于逐条催办。"""

    def __init__(self, blockers: list[str]):
        self.blockers = blockers
        super().__init__("；".join(blockers))
