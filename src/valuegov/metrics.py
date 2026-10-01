"""贡献公式引擎：口径校验、期间汇总与指标计算。

公式以版本化的结构化定义保存，报告期关账时随快照冻结；引擎只认识
已登记的数据来源与过滤条件，保证任何版本的公式都可被确定性地重算。
"""

from __future__ import annotations

from .domain import to_major
from .errors import ValidationError

RATIO = "ratio"
SUM = "sum"
GROWTH = "growth"
_KINDS = (RATIO, SUM, GROWTH)

SOURCES = frozenset(
    {
        "transformation_value_added",
        "revenue",
        "rd_expense",
        "public_service_value",
        "investment",
    }
)

_SIDE_KEYS = {"source", "se_only", "basic_only", "eliminate"}


def _validate_side(side, metric: str, label: str) -> None:
    if not isinstance(side, dict) or not set(side) <= _SIDE_KEYS or "source" not in side:
        raise ValidationError(f"指标{metric}的{label}口径定义无效")
    source = side["source"]
    if source not in SOURCES:
        raise ValidationError(f"指标{metric}的{label}引用了未知数据来源: {source}")
    if side.get("se_only") and source not in ("transformation_value_added", "revenue"):
        raise ValidationError(f"指标{metric}的{label}：se_only 仅适用于转化/收入类来源")
    if side.get("basic_only") and source != "rd_expense":
        raise ValidationError(f"指标{metric}的{label}：basic_only 仅适用于研发费用")
    if side.get("eliminate") and source not in ("transformation_value_added", "revenue"):
        raise ValidationError(f"指标{metric}的{label}：eliminate 仅适用于转化/收入类来源")


def validate_formula(definition):
    """校验公式定义结构，通过则原样返回。"""
    if not isinstance(definition, dict) or set(definition) != {"metrics"}:
        raise ValidationError("公式定义必须且仅包含 metrics")
    metrics = definition["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        raise ValidationError("公式指标不能为空")
    for name, spec in metrics.items():
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("指标名称无效")
        if not isinstance(spec, dict):
            raise ValidationError(f"指标{name}定义无效")
        kind = spec.get("kind")
        if kind not in _KINDS:
            raise ValidationError(f"指标{name}类型无效: {kind}")
        if kind == RATIO:
            if set(spec) != {"kind", "numerator", "denominator"}:
                raise ValidationError(f"指标{name}的比率定义必须包含 numerator 与 denominator")
            _validate_side(spec["numerator"], name, "分子")
            _validate_side(spec["denominator"], name, "分母")
        else:
            if not set(spec) <= _SIDE_KEYS | {"kind"} or "source" not in spec:
                raise ValidationError(f"指标{name}定义无效")
            _validate_side({k: v for k, v in spec.items() if k != "kind"}, name, "汇总")
    return definition


def compute_figures(store, period: str, org_scope: set, unit_map: dict) -> dict:
    """按组织范围汇总指定报告期的活动记录（仅最新版本且未作废）。

    org_scope 为当期集团组织边界内的单位集合；unit_map 把已重组单位
    映射到其承继单位。金额一律为百元整数。
    """
    projects = {}
    for row in store.qa(
        "SELECT p.project_id, p.owner_unit_id, c.is_strategic_emerging AS se "
        "FROM projects p LEFT JOIN industry_categories c ON c.category_id = p.category_id"
    ):
        projects[row["project_id"]] = (row["owner_unit_id"], bool(row["se"]))

    def in_scope(owner_id) -> bool:
        return owner_id is not None and unit_map.get(owner_id, owner_id) in org_scope

    figures = {
        "transformation_value_added": 0,
        "transformation_value_added_se": 0,
        "revenue": 0,
        "revenue_se": 0,
        "rd_expense": 0,
        "rd_expense_basic": 0,
        "public_service_value": 0,
        "investment": 0,
        "elim_value_added": 0,
        "elim_value_added_se": 0,
        "elim_amount": 0,
    }

    for row in store.qa(
        "SELECT project_id, revenue_minor, value_added_minor FROM transformations "
        "WHERE period=? AND superseded_by IS NULL AND void=0",
        (period,),
    ):
        owner, se = projects.get(row["project_id"], (None, False))
        if not in_scope(owner):
            continue
        figures["revenue"] += row["revenue_minor"]
        figures["transformation_value_added"] += row["value_added_minor"]
        if se:
            figures["revenue_se"] += row["revenue_minor"]
            figures["transformation_value_added_se"] += row["value_added_minor"]

    for row in store.qa(
        "SELECT project_id, amount_minor, is_basic_research FROM rd_expenses "
        "WHERE period=? AND superseded_by IS NULL AND void=0",
        (period,),
    ):
        owner, _se = projects.get(row["project_id"], (None, False))
        if not in_scope(owner):
            continue
        figures["rd_expense"] += row["amount_minor"]
        if row["is_basic_research"]:
            figures["rd_expense_basic"] += row["amount_minor"]

    for row in store.qa(
        "SELECT project_id, amount_minor FROM investment_batches "
        "WHERE period=? AND superseded_by IS NULL AND void=0",
        (period,),
    ):
        owner, _se = projects.get(row["project_id"], (None, False))
        if in_scope(owner):
            figures["investment"] += row["amount_minor"]

    for row in store.qa(
        "SELECT unit_id, value_minor FROM public_service_contributions "
        "WHERE period=? AND superseded_by IS NULL AND void=0",
        (period,),
    ):
        if in_scope(row["unit_id"]):
            figures["public_service_value"] += row["value_minor"]

    for row in store.qa(
        "SELECT e.amount_minor, e.value_added_minor, t.project_id "
        "FROM elimination_entries e "
        "JOIN elimination_runs r ON r.run_id = e.run_id "
        "JOIN internal_transactions t ON t.txn_id = e.txn_id "
        "WHERE r.period = ?",
        (period,),
    ):
        figures["elim_amount"] += row["amount_minor"]
        figures["elim_value_added"] += row["value_added_minor"]
        if row["project_id"]:
            _owner, se = projects.get(row["project_id"], (None, False))
            if se:
                figures["elim_value_added_se"] += row["value_added_minor"]

    return figures


def _side_value(spec: dict, figures: dict) -> int:
    source = spec["source"]
    if source == "transformation_value_added":
        key = "transformation_value_added_se" if spec.get("se_only") else "transformation_value_added"
        value = figures[key]
        if spec.get("eliminate"):
            value -= figures["elim_value_added_se" if spec.get("se_only") else "elim_value_added"]
        return value
    if source == "revenue":
        value = figures["revenue_se" if spec.get("se_only") else "revenue"]
        if spec.get("eliminate"):
            value -= figures["elim_amount"]
        return value
    if source == "rd_expense":
        return figures["rd_expense_basic" if spec.get("basic_only") else "rd_expense"]
    if source == "public_service_value":
        return figures["public_service_value"]
    if source == "investment":
        return figures["investment"]
    raise ValidationError(f"未知数据来源: {source}")


def evaluate(definition: dict, figures: dict, prior_figures: dict | None = None) -> dict:
    """按公式定义计算各项指标；比率与增长率为小数，汇总指标为万元。"""
    results = {}
    for name, spec in definition["metrics"].items():
        kind = spec["kind"]
        if kind == RATIO:
            denominator = _side_value(spec["denominator"], figures)
            numerator = _side_value(spec["numerator"], figures)
            results[name] = None if denominator == 0 else numerator / denominator
        elif kind == SUM:
            results[name] = to_major(_side_value(spec, figures))
        elif kind == GROWTH:
            current = _side_value(spec, figures)
            previous = _side_value(spec, prior_figures) if prior_figures else 0
            results[name] = None if not previous else (current - previous) / previous
        else:  # pragma: no cover - 已在 validate_formula 拦截
            raise ValidationError(f"未知指标类型: {kind}")
    return results
