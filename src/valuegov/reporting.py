"""管理层视图：占比变化、目标差额、项目明细、费用归属、抵销过程与审批责任。

已关账期间展示快照冻结的口径与结果，并同时给出纳入关账后更正版本的
重述视图，保证管理层看到的是“真实差额”。
"""

from __future__ import annotations

import json

from .domain import to_major
from .errors import NotFoundError


def _round(value, digits=6):
    return None if value is None else round(value, digits)


def _figures_major(figures: dict) -> dict:
    return {key: to_major(value) for key, value in figures.items()}


def _metric_rows(definition: dict, metrics: dict, targets: dict, previous: dict | None) -> list:
    rows = []
    for name, spec in definition["metrics"].items():
        actual = metrics.get(name)
        target = targets.get(name)
        prev = previous.get(name) if previous else None
        gap = actual - target if actual is not None and target is not None else None
        delta = actual - prev if actual is not None and prev is not None else None
        rows.append(
            {
                "metric": name,
                "kind": spec["kind"],
                "actual": _round(actual),
                "target": _round(target),
                "gap": _round(gap),
                "gap_points": _round(gap * 100, 4) if gap is not None and spec["kind"] == "ratio" else _round(gap),
                "previous": _round(prev),
                "delta": _round(delta),
            }
        )
    return rows


def _live_view(service, period: str) -> dict:
    version, definition = service._latest_formula()
    metrics, figures = service._metrics_for(period, definition)
    return {
        "formula_version": version,
        "definition": definition,
        "metrics": metrics,
        "figures": figures,
        "targets": service._active_targets(period),
    }


def _snapshot_view(service, snapshot_row) -> dict:
    results = json.loads(snapshot_row["results"])
    formula = service.store.q1(
        "SELECT definition FROM formula_versions WHERE version=?", (snapshot_row["formula_version"],)
    )
    return {
        "formula_version": snapshot_row["formula_version"],
        "definition": json.loads(formula["definition"]),
        "metrics": results["metrics"],
        "figures": results["figures"],
        "targets": json.loads(snapshot_row["targets"]),
        "org_scope": json.loads(snapshot_row["org_scope"]),
    }


def _previous_metrics(service, period: str) -> dict | None:
    row = service.store.q1(
        "SELECT period, status, snapshot_id FROM periods WHERE period<? ORDER BY period DESC LIMIT 1",
        (period,),
    )
    if not row:
        return None
    if row["status"] == "CLOSED":
        snapshot = service.store.q1("SELECT * FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],))
        return json.loads(snapshot["results"])["metrics"]
    _version, definition = service._latest_formula()
    metrics, _figures = service._metrics_for(row["period"], definition)
    return metrics


def _project_drilldown(service, period: str, scope: set, unit_map: dict) -> list:
    store = service.store
    transformations = {
        r["project_id"]: r
        for r in store.qa(
            "SELECT project_id, SUM(revenue_minor) AS revenue, SUM(value_added_minor) AS value_added "
            "FROM transformations WHERE period=? AND superseded_by IS NULL AND void=0 GROUP BY project_id",
            (period,),
        )
    }
    expenses = {
        r["project_id"]: r
        for r in store.qa(
            "SELECT project_id, SUM(amount_minor) AS total, "
            "SUM(CASE WHEN is_basic_research=1 THEN amount_minor ELSE 0 END) AS basic "
            "FROM rd_expenses WHERE period=? AND superseded_by IS NULL AND void=0 GROUP BY project_id",
            (period,),
        )
    }
    investments = {
        r["project_id"]: r
        for r in store.qa(
            "SELECT project_id, SUM(amount_minor) AS total FROM investment_batches "
            "WHERE period=? AND superseded_by IS NULL AND void=0 GROUP BY project_id",
            (period,),
        )
    }
    unit_cache = {}

    def unit_code(unit_id: str) -> str:
        if unit_id not in unit_cache:
            row = store.q1("SELECT code, name FROM org_units WHERE unit_id=?", (unit_id,))
            unit_cache[unit_id] = (row["code"], row["name"]) if row else (unit_id, unit_id)
        return unit_cache[unit_id]

    drilldown = []
    for project_id in sorted(set(transformations) | set(expenses) | set(investments)):
        project = store.q1("SELECT * FROM projects WHERE project_id=?", (project_id,))
        owner = unit_map.get(project["owner_unit_id"], project["owner_unit_id"])
        if owner not in scope:
            continue
        category = None
        if project["category_id"]:
            category = store.q1(
                "SELECT code, name, is_strategic_emerging FROM industry_categories WHERE category_id=?",
                (project["category_id"],),
            )
        rules = store.qa(
            "SELECT unit_id, weight, basis FROM allocation_rules "
            "WHERE project_id=? AND effective_period=? AND superseded_by IS NULL",
            (project_id, period),
        )
        shares: dict = {}
        if rules:
            for rule in rules:
                mapped = unit_map.get(rule["unit_id"], rule["unit_id"])
                entry = shares.setdefault(mapped, {"weight": 0.0, "basis": []})
                entry["weight"] += rule["weight"]
                if rule["basis"] not in entry["basis"]:
                    entry["basis"].append(rule["basis"])
        else:
            shares[owner] = {"weight": 1.0, "basis": ["业主单位默认"]}
        trans = transformations.get(project_id)
        expense = expenses.get(project_id)
        investment = investments.get(project_id)
        value_added = trans["value_added"] if trans else 0
        revenue = trans["revenue"] if trans else 0
        rd_total = expense["total"] if expense else 0
        rd_basic = expense["basic"] if expense else 0
        invested = investment["total"] if investment else 0
        units = []
        for unit_id, share in sorted(shares.items(), key=lambda item: unit_code(item[0])[0]):
            code, name = unit_code(unit_id)
            units.append(
                {
                    "unit_code": code,
                    "unit_name": name,
                    "weight": round(share["weight"], 6),
                    "basis": "；".join(share["basis"]),
                    "value_added": to_major(round(value_added * share["weight"])),
                    "revenue": to_major(round(revenue * share["weight"])),
                    "rd_expense": to_major(round(rd_total * share["weight"])),
                }
            )
        drilldown.append(
            {
                "project_id": project_id,
                "code": project["code"],
                "name": project["name"],
                "owner_unit_code": unit_code(owner)[0],
                "category_code": category["code"] if category else None,
                "is_strategic_emerging": bool(category["is_strategic_emerging"]) if category else False,
                "classification_pending": project["pending_category_id"] is not None,
                "revenue": to_major(revenue),
                "value_added": to_major(value_added),
                "rd_expense": to_major(rd_total),
                "basic_research_expense": to_major(rd_basic),
                "investment": to_major(invested),
                "units": units,
            }
        )
    return drilldown


def _elimination_view(service, period: str) -> dict:
    store = service.store
    runs = []
    for run in store.qa("SELECT * FROM elimination_runs WHERE period=? ORDER BY created_at", (period,)):
        entries = []
        for entry in store.qa(
            "SELECT * FROM elimination_entries WHERE run_id=? ORDER BY entry_id", (run["run_id"],)
        ):
            txn = store.q1("SELECT * FROM internal_transactions WHERE txn_id=?", (entry["txn_id"],))
            seller = store.q1("SELECT code FROM org_units WHERE unit_id=?", (txn["seller_unit_id"],))
            buyer = store.q1("SELECT code FROM org_units WHERE unit_id=?", (txn["buyer_unit_id"],))
            project = None
            if txn["project_id"]:
                project = store.q1("SELECT code FROM projects WHERE project_id=?", (txn["project_id"],))
            entries.append(
                {
                    "entry_id": entry["entry_id"],
                    "txn_id": entry["txn_id"],
                    "seller_unit_code": seller["code"] if seller else None,
                    "buyer_unit_code": buyer["code"] if buyer else None,
                    "project_code": project["code"] if project else None,
                    "amount": to_major(entry["amount_minor"]),
                    "value_added": to_major(entry["value_added_minor"]),
                }
            )
        runs.append(
            {
                "run_id": run["run_id"],
                "created_by": run["created_by"],
                "created_at": run["created_at"],
                "entries": entries,
            }
        )
    open_txns = []
    for txn in store.qa(
        "SELECT * FROM internal_transactions WHERE period=? AND status='OPEN' "
        "AND superseded_by IS NULL AND void=0 ORDER BY created_at",
        (period,),
    ):
        seller = store.q1("SELECT code FROM org_units WHERE unit_id=?", (txn["seller_unit_id"],))
        buyer = store.q1("SELECT code FROM org_units WHERE unit_id=?", (txn["buyer_unit_id"],))
        open_txns.append(
            {
                "txn_id": txn["txn_id"],
                "seller_unit_code": seller["code"] if seller else None,
                "buyer_unit_code": buyer["code"] if buyer else None,
                "amount": to_major(txn["amount_minor"]),
                "value_added": to_major(txn["value_added_minor"]),
            }
        )
    return {"runs": runs, "open_transactions": open_txns}


def _approval_view(service, period: str) -> list:
    store = service.store
    view = []
    for row in store.qa(
        "SELECT * FROM evidence WHERE period=? OR period IS NULL ORDER BY submitted_at", (period,)
    ):
        view.append(
            {
                "kind": "evidence",
                "id": row["evidence_id"],
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "status": row["status"],
                "submitted_by": row["submitted_by"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"],
                "decision_note": row["decision_note"],
            }
        )
    for row in store.qa("SELECT * FROM corrections WHERE period=? ORDER BY created_at", (period,)):
        view.append(
            {
                "kind": "correction",
                "id": row["correction_id"],
                "subject_type": row["entity_type"],
                "subject_id": row["entity_id"],
                "status": row["status"],
                "submitted_by": row["created_by"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"],
                "decision_note": row["reason"],
            }
        )
    return view


def period_report(service, period: str) -> dict:
    """单个报告期的完整管理视图。"""
    row = service._period_row(period)
    if not row:
        raise NotFoundError(f"报告期不存在: {period}")
    frozen = row["status"] == "CLOSED"
    restated = None
    corrections_after_close = 0
    if frozen:
        snapshot = service.store.q1("SELECT * FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],))
        view = _snapshot_view(service, snapshot)
        live = _live_view(service, period)
        corrections_after_close = service.store.q1(
            "SELECT COUNT(*) AS n FROM corrections c "
            "JOIN audit_log a ON a.action='CORRECTION_APPROVE' AND a.entity_id=c.correction_id "
            "WHERE c.period=? AND c.status='APPROVED' AND a.entry_id > ?",
            (period, snapshot["closed_seq"]),
        )["n"]
        restated = {
            "formula_version": live["formula_version"],
            "metrics": {k: _round(v) for k, v in live["metrics"].items()},
            "figures": _figures_major(live["figures"]),
            "targets": live["targets"],
        }
        scope = set(view["org_scope"])
    else:
        view = _live_view(service, period)
        scope = service._org_scope(period)
    previous = _previous_metrics(service, period)
    unit_map = service._unit_map(period)
    return {
        "period": period,
        "status": row["status"],
        "frozen": frozen,
        "snapshot_id": row["snapshot_id"] if frozen else None,
        "formula_version": view["formula_version"],
        "metrics": _metric_rows(view["definition"], view["metrics"], view["targets"], previous),
        "figures": _figures_major(view["figures"]),
        "projects": _project_drilldown(service, period, scope, unit_map),
        "eliminations": _elimination_view(service, period),
        "approvals": _approval_view(service, period),
        "corrections_after_close": corrections_after_close,
        "restated": restated,
    }


def ratio_trend(service, periods: list) -> list:
    """多个报告期的指标走势：实际值、目标与真实差额。"""
    trend = []
    for period in periods:
        row = service._period_row(period)
        if not row:
            raise NotFoundError(f"报告期不存在: {period}")
        if row["status"] == "CLOSED":
            snapshot = service.store.q1("SELECT * FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],))
            view = _snapshot_view(service, snapshot)
        else:
            view = _live_view(service, period)
        metrics = {}
        for name in view["definition"]["metrics"]:
            actual = view["metrics"].get(name)
            target = view["targets"].get(name)
            metrics[name] = {
                "actual": _round(actual),
                "target": _round(target),
                "gap": _round(actual - target) if actual is not None and target is not None else None,
            }
        trend.append({"period": period, "status": row["status"], "metrics": metrics})
    return trend
