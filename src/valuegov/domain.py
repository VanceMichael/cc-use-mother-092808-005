"""共享常量、标识与数值工具。

金额约定：对外接口以“万元”为单位的数值，内部一律换算成“百元”整数
（万元的百分之一）存储与汇总，避免浮点误差影响占比核实。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import ValidationError

ROLE_ADMIN = "ADMIN"
ROLE_REVIEWER = "REVIEWER"
ROLE_PLANNER = "PLANNER"
ROLE_SUBMITTER = "UNIT_SUBMITTER"
ALL_ROLES = frozenset({ROLE_ADMIN, ROLE_REVIEWER, ROLE_PLANNER, ROLE_SUBMITTER})

PERIOD_RE = re.compile(r"^\d{4}(-(Q[1-4]|H[12]|M(0[1-9]|1[0-2])))?$")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now_iso(clock) -> str:
    return clock().astimezone(timezone.utc).isoformat()


def parse_instant(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValidationError(f"时间格式无效: {text!r}") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def validate_period(period) -> str:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValidationError(f"报告期格式无效: {period!r}")
    return period


def prior_period(period: str) -> str | None:
    """年度期间的上一年，用于研发经费增长率；其他粒度暂不推导。"""
    if PERIOD_RE.fullmatch(period) and len(period) == 4:
        return f"{int(period) - 1:04d}"
    return None


def to_minor(value) -> int:
    """把以万元为单位的金额换算成百元整数。"""
    if isinstance(value, bool):
        raise ValidationError("金额必须是数值")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"金额格式无效: {value!r}") from None
    if not amount.is_finite():
        raise ValidationError("金额必须是有限数值")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_major(minor: int) -> float:
    return minor / 100


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
