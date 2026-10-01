"""贡献归集计算引擎。

引擎不触碰数据库：输入是一个可被快照冻结的 ``dataset`` 字典，输出占比、
抵销过程与钻取明细。这样关账时点的公式与数据可以原样重放。

归集规则（与领域约束一一对应）：

1. 同一分摊作用域（项目 / 项目+任务 / 项目+任务+批次）内多个单位报送的
   金额先汇总为总额，再按经审批的分摊规则份额分配到单位；无规则时归报送
   单位。作用域内存在多个报送单位却没有规则，属于未决的联合贡献。
2. 重组按生效事件把源单位贡献依权重平移到目标单位（支持链式重组）。
3. 项目拆分下子项目独立归集，跨期比较时按拆分权重还原父项目口径。
4. 战新认定与基础研究属性必须有已批准的特殊归类，仅申请不计数。
5. 仅 MATCHED 的集团内部交易参与抵销，按项目战新属性分别冲减增加值与
   收入；PENDING / DISPUTED 交易不得关账。
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# 公式版本随算法变更而递增；关账快照记录该版本，历史版本永不被重算
FORMULA_VERSION = "2030-plan-v1"

CENT = Decimal("0.01")
SHARE_Q = Decimal("0.0001")
RATIO_Q = Decimal("0.000001")


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def q_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def q_share(value: Decimal) -> Decimal:
    return value.quantize(SHARE_Q, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# 数据装配
# --------------------------------------------------------------------------
def _rule_sort_key(rule: dict[str, Any]) -> tuple[int, int, int]:
    return (
        1 if rule.get("task_code") else 0,
        1 if rule.get("batch_code") else 0,
        rule["version"],
    )


def _index_rules(dataset: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
    """每个 (project, task|'', batch|'') 作用域取粒度最细的生效规则集。"""
    best: dict[tuple, dict[str, Any]] = {}
    for rule in dataset["rules"]:
        key = (
            rule["project_code"],
            rule.get("task_code") or "",
            rule.get("batch_code") or "",
        )
        current = best.get(key)
        if current is None or _rule_sort_key(rule) > _rule_sort_key(current):
            best[key] = rule
    return best


def _resolve_rule(
    rules_by_key: dict[tuple, dict[str, Any]],
    project_code: str,
    task_code: str | None,
    batch_code: str | None,
) -> dict[str, Any] | None:
    """从最细粒度逐级回退：项目+任务+批次 → 项目+任务 → 项目。"""
    candidates = [
        (project_code, task_code or "", batch_code or ""),
        (project_code, task_code or "", ""),
        (project_code, "", ""),
    ]
    for key in candidates:
        rule = rules_by_key.get(key)
        if rule is not None:
            return rule
    return None


def _line_scope(line: dict[str, Any]) -> tuple[str, str, str]:
    return (
        line["project_code"],
        line.get("task_code") or "",
        line.get("batch_code") or "",
    )


def find_unallocated_joint_scopes(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """多单位报送却没有分摊规则的作用域——联合贡献被重复计算的风险点。"""
    rules_by_key = _index_rules(dataset)
    reporters: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for section in ("outputs", "rd_expenses", "transformations"):
        for line in dataset[section]:
            key = (section,) + _line_scope(line)
            reporters[key].add(line["reported_org"])
    problems: list[dict[str, Any]] = []
    for (section, project, task, batch), orgs in sorted(reporters.items()):
        if len(orgs) <= 1:
            continue
        if _resolve_rule(rules_by_key, project, task or None, batch or None) is None:
            problems.append(
                {
                    "section": section,
                    "project_code": project,
                    "task_code": task or None,
                    "batch_code": batch or None,
                    "reporters": sorted(orgs),
                }
            )
    return problems


# --------------------------------------------------------------------------
# 分摊与重组
# --------------------------------------------------------------------------
def attribute_lines(
    dataset: dict[str, Any], section: str
) -> list[dict[str, Any]]:
    """把某类贡献记录按规则归集到单位。

    作用域（项目 / 项目+任务 / 项目+任务+批次）内：

    - 无分摊规则：每行归报送单位；多单位申报会被关账阻断（见
      :func:`find_unallocated_joint_scopes`）。
    - 有分摊规则：作用域毛额先汇总各方申报，再统一按经批准份额分配到
      单位（末位承接一分钱舍入差）。这样规则才是唯一的归集口径，
      任何一方"把整个项目算成自己新增价值"都不会改变集团总额；申报
      构成与批准份额不符时由关账阻断（见
      :func:`find_claim_share_mismatches`）。
    """
    rules_by_key = _index_rules(dataset)
    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for line in dataset[section]:
        grouped[_line_scope(line)].append(line)

    results: list[dict[str, Any]] = []
    for scope, lines in grouped.items():
        project_code, task_code, batch_code = scope
        amount_fields = [
            f for f in ("revenue", "value_added", "amount") if f in lines[0]
        ]
        reporters = sorted({line["reported_org"] for line in lines})
        gross = {
            f: q_money(sum((D(line[f]) for line in lines), Decimal("0")))
            for f in amount_fields
        }
        # 获批特殊属性口径（战新产出 / 基础研究费用），按相同份额归集
        flag_name = "basic" if section == "rd_expenses" else "strategic"
        flagged_value_field = (
            "amount" if section == "rd_expenses" else "value_added"
        )
        if flagged_value_field in amount_fields:
            flagged_gross = q_money(sum(
                (D(line[flagged_value_field]) for line in lines
                 if line.get(flag_name, False)),
                Decimal("0"),
            ))
        else:
            flagged_gross = Decimal("0")
        gross_by_reporter: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for line in lines:
            for f in amount_fields:
                gross_by_reporter[line["reported_org"]].setdefault(f, Decimal("0"))
                gross_by_reporter[line["reported_org"]][f] += q_money(D(line[f]))
        flags = {
            "strategic": any(bool(line.get("strategic", False)) for line in lines),
            "basic": any(bool(line.get("basic", False)) for line in lines),
        }
        rule = _resolve_rule(rules_by_key, project_code,
                             task_code or None, batch_code or None)
        if rule is None:
            # 无规则：保留各方原申报；多单位申报时按金额构成给展示份额，
            # 但该作用域会被关账阻断（联合贡献未归一）
            allocations = []
            for org in reporters:
                entry: dict[str, Any] = {"org_code": org}
                for f in amount_fields:
                    entry[f] = q_money(
                        gross_by_reporter[org].get(f, Decimal("0"))
                    )
                base_field = amount_fields[0]
                entry["share"] = (
                    (entry[base_field] / gross[base_field])
                    if gross[base_field]
                    else Decimal("0")
                )
                allocations.append(entry)
            flagged_allocations = {}
            for org in reporters:
                flagged_allocations[org] = q_money(
                    sum(
                        (D(line[flagged_value_field]) for line in lines
                         if line["reported_org"] == org
                         and line.get(flag_name, False)),
                        Decimal("0"),
                    )
                )
            rule_id, basis = None, "SELF"
        else:
            shares = [
                (s["org_code"], D(s["share"])) for s in rule["shares"]
            ]
            allocations = []
            flagged_allocations = {}
            for index, (org, share) in enumerate(shares):
                entry: dict[str, Any] = {"org_code": org, "share": share}
                for f in amount_fields:
                    if index < len(shares) - 1:
                        entry[f] = q_money(gross[f] * share)
                    else:
                        entry[f] = q_money(
                            gross[f]
                            - sum(
                                q_money(gross[f] * sh)
                                for _, sh in shares[:-1]
                            )
                        )
                allocations.append(entry)
                flagged_allocations[org] = (
                    q_money(flagged_gross * share)
                    if index < len(shares) - 1
                    else q_money(
                        flagged_gross
                        - sum(
                            q_money(flagged_gross * sh)
                            for _, sh in shares[:-1]
                        )
                    )
                )
            rule_id, basis = rule["id"], rule["basis"]
        results.append(
            {
                "section": section,
                "scope": {
                    "project_code": project_code,
                    "task_code": task_code or None,
                    "batch_code": batch_code or None,
                },
                "project_code": project_code,
                "task_code": task_code or None,
                "batch_code": batch_code or None,
                "reported_org": reporters[0] if len(reporters) == 1 else None,
                "reporters": reporters,
                "source_ids": [line["id"] for line in lines],
                "rule_set_id": rule_id,
                "allocation_basis": basis,
                "gross": gross,
                "gross_by_reporter": {k: dict(v) for k, v in
                                      gross_by_reporter.items()},
                "flagged_field": flagged_value_field,
                "flagged_gross": str(flagged_gross),
                "flagged_allocations": {k: str(v) for k, v in
                                        flagged_allocations.items()},
                "allocations": allocations,
                "flags": flags,
            }
        )
    return results


def apply_reorgs(
    dataset: dict[str, Any], attributed: list[dict[str, Any]]
) -> None:
    """按重组事件把贡献从源单位平移到目标单位，事件按生效期顺序执行。

    源单位份额按权重拆成目标单位份额（末位承接舍入差，合计恒等），金额
    依拆分后的份额重算，最后一分钱计入末位，保证平移前后金额分文不差。
    """
    events = sorted(
        dataset.get("reorg_events", []), key=lambda e: e["effective_period"]
    )
    mappings = dataset.get("reorg_mappings", [])
    for event in events:
        event_mappings = [m for m in mappings if m["event_id"] == event["id"]]
        source_orgs = {m["source_org"] for m in event_mappings}
        targets_by_source: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
        for m in event_mappings:
            targets_by_source[m["source_org"]].append((m["target_org"], D(m["weight"])))
        for row in attributed:
            moved = [a for a in row["allocations"] if a["org_code"] in source_orgs]
            for allocation in moved:
                source_org = allocation["org_code"]
                share = D(allocation["share"])
                targets = targets_by_source[source_org]
                share_pieces = [q_share(share * w) for _, w in targets[:-1]]
                share_pieces.append(share - sum(share_pieces))
                money_fields = [
                    f for f in ("value_added", "revenue", "amount")
                    if f in allocation
                ]
                new_entries: list[dict[str, Any]] = []
                for index, ((target_org, _), piece) in enumerate(
                    zip(targets, share_pieces)
                ):
                    entry: dict[str, Any] = {
                        "org_code": target_org,
                        "share": piece,
                        "_via_reorg_from": source_org,
                    }
                    for field in money_fields:
                        entry[field] = q_money(D(row["gross"][field]) * piece)
                    new_entries.append(entry)
                # 末位承接各金额字段的舍入差，平移前后分文不差
                for field in money_fields:
                    original = q_money(D(allocation[field]))
                    new_entries[-1][field] = q_money(
                        original - sum(D(e[field]) for e in new_entries[:-1])
                    )
                row["allocations"].remove(allocation)
                row["allocations"].extend(new_entries)


def _project_strategic(dataset: dict[str, Any], project_code: str) -> bool:
    project = dataset["projects_by_code"][project_code]
    industry = dataset["industry_by_code"][project["industry_code"]]
    return bool(industry["is_strategic"])


# --------------------------------------------------------------------------
# 待办阻断
# --------------------------------------------------------------------------
def find_claim_share_mismatches(dataset: dict[str, Any]
                                ) -> list[dict[str, str]]:
    """申报构成与经审批分摊份额不一致的作用域。

    典型情形：产业板块、研究院、公共服务单位就同一项目各报全额，申报
    构成约为 1/3、1/3、1/3，与批准的份额（如 0.5/0.3/0.2）不符。此类
    数据"无法核实"，必须重新申报或修改规则后才能关账。
    """
    rules_by_key = _index_rules(dataset)
    # (section, project, task, batch) -> {org: 申报金额}
    claims: dict[tuple, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for section in ("outputs", "rd_expenses", "transformations"):
        value_field = "amount" if section == "rd_expenses" else "value_added"
        for line in dataset[section]:
            key = (section,) + _line_scope(line)
            claims[key][line["reported_org"]] += D(line[value_field])

    mismatches: list[dict[str, str]] = []
    for (section, project, task, batch), by_org in sorted(claims.items()):
        if len(by_org) < 2:
            # 单一单位申报整项目时，直接按批准份额拆分，不构成冲突
            continue
        rule = _resolve_rule(rules_by_key, project, task or None,
                             batch or None)
        if rule is None:
            continue  # 无规则情形由联合贡献阻断项处理
        approved = {s["org_code"]: D(s["share"]) for s in rule["shares"]}
        total = sum(by_org.values())
        if total == 0:
            continue
        for org, amount in by_org.items():
            if org not in approved:
                mismatches.append(
                    {
                        "message": (
                            f"{section} 项目{project}存在未纳入分摊规则的"
                            f"申报单位{org}（规则单位：{','.join(sorted(approved))}）"
                        ),
                        "target_org": org,
                    }
                )
                continue
            claimed_share = amount / total
            if abs(claimed_share - approved[org]) > Decimal("0.01"):
                mismatches.append(
                    {
                        "message": (
                            f"{section} 项目{project}的单位{org}申报构成"
                            f"{claimed_share.quantize(SHARE_Q)}与批准份额"
                            f"{approved[org]}不一致，须重新申报或修订规则"
                        ),
                        "target_org": org,
                    }
                )
    return mismatches


def find_blockers(dataset: dict[str, Any]) -> list[dict[str, str]]:
    """返回关账前全部阻断项（消息 + 责任单位）；空列表表示可以关账。"""
    blockers: list[dict[str, str]] = []
    blockers.extend(find_claim_share_mismatches(dataset))
    for scope in find_unallocated_joint_scopes(dataset):
        blockers.append(
            {
                "message": (
                    f"联合贡献缺少分摊规则：{scope['section']} 项目"
                    f"{scope['project_code']}（报送单位 "
                    f"{','.join(scope['reporters'])}）"
                ),
                "target_org": scope["reporters"][0],
            }
        )
    for line in dataset["outputs"]:
        if line.get("claimed_strategic") and not line.get("strategic"):
            blockers.append(
                {
                    "message": (
                        f"产出记录{line['id']}（项目{line['project_code']}）"
                        "申请战新认定但缺少已批准证据"
                    ),
                    "target_org": line["reported_org"],
                }
            )
    for line in dataset["rd_expenses"]:
        if line.get("basic_claim") and not line.get("basic"):
            blockers.append(
                {
                    "message": (
                        f"研发费用记录{line['id']}（项目{line['project_code']}）"
                        "申请基础研究属性但缺少已批准证据"
                    ),
                    "target_org": line["reported_org"],
                }
            )
    for trade in dataset["internal_trades"]:
        if trade["status"] == "PENDING":
            blockers.append(
                {
                    "message": (
                        f"内部交易{trade['id']}（{trade['seller_org']}→"
                        f"{trade['buyer_org']}）未经双边确认"
                    ),
                    "target_org": trade["seller_org"],
                }
            )
        elif trade["status"] == "DISPUTED":
            blockers.append(
                {
                    "message": (
                        f"内部交易{trade['id']}存在争议未裁决："
                        f"{trade.get('dispute_note', '')}"
                    ),
                    "target_org": trade["seller_org"],
                }
            )
    return blockers


# --------------------------------------------------------------------------
# 主计算
# --------------------------------------------------------------------------
def compute_report(dataset: dict[str, Any]) -> dict[str, Any]:
    """根据冻结数据集计算占比、抵销、审批责任与目标差额。"""
    for project in dataset["projects"]:
        dataset.setdefault("projects_by_code", {})[project["code"]] = project
    for industry in dataset["industries"]:
        dataset.setdefault("industry_by_code", {})[industry["code"]] = industry

    attributed: dict[str, list[dict[str, Any]]] = {}
    for section in ("outputs", "rd_expenses", "transformations", "public_services"):
        rows = attribute_lines(dataset, section)
        apply_reorgs(dataset, rows)
        attributed[section] = rows

    gross_va = Decimal("0")
    gross_revenue = Decimal("0")
    strategic_va = Decimal("0")
    for section in ("outputs", "transformations"):
        for row in attributed[section]:
            gross_va += row["gross"]["value_added"]
            gross_revenue += row["gross"]["revenue"]
            if _project_strategic(dataset, row["project_code"]):
                # 项目产业目录为战新（含经批准的产业重分类）：全部增加值计战新
                strategic_va += row["gross"]["value_added"]
            else:
                # 非战新项目中经逐笔批准的战新产出
                strategic_va += D(row["flagged_gross"])

    # 内部交易抵销：仅双边确认的交易，按项目战新属性冲减
    eliminations: list[dict[str, Any]] = []
    eliminated_va = Decimal("0")
    eliminated_strategic_va = Decimal("0")
    eliminated_revenue = Decimal("0")
    for trade in dataset["internal_trades"]:
        if trade["status"] != "MATCHED":
            continue
        va = q_money(D(trade["value_added_amount"]))
        revenue = q_money(D(trade["revenue_amount"]))
        is_strategic = (
            trade.get("project_code") is not None
            and _project_strategic(dataset, trade["project_code"])
        )
        eliminated_va += va
        eliminated_revenue += revenue
        if is_strategic:
            eliminated_strategic_va += va
        eliminations.append(
            {
                "trade_id": trade["id"],
                "project_code": trade.get("project_code"),
                "seller_org": trade["seller_org"],
                "buyer_org": trade["buyer_org"],
                "revenue": str(revenue),
                "value_added": str(va),
                "strategic": is_strategic,
                "confirmed_by": trade.get("confirmed_by"),
            }
        )

    net_va = gross_va - eliminated_va
    net_strategic_va = strategic_va - eliminated_strategic_va
    net_revenue = gross_revenue - eliminated_revenue

    # 研发费用与基础研究：作用域毛额（重组只平移归属，不改变总额）
    rd_total = sum(
        (row["gross"]["amount"] for row in attributed["rd_expenses"]),
        Decimal("0"),
    )
    basic_total = sum(
        (D(row["flagged_gross"]) for row in attributed["rd_expenses"]),
        Decimal("0"),
    )
    public_total = sum(
        (row["gross"]["amount"] for row in attributed["public_services"]),
        Decimal("0"),
    )

    strategic_ratio = (net_strategic_va / net_va) if net_va else Decimal("0")
    basic_ratio = (basic_total / rd_total) if rd_total else Decimal("0")

    # 研发增速：与上一期关账快照比较
    prior = dataset.get("prior_totals") or {}
    prior_rd = D(prior["rd_total"]) if prior.get("rd_total") else None
    rd_growth = ((rd_total / prior_rd) - 1) if prior_rd else None

    goals = {g["metric"]: g for g in dataset.get("goals", [])}
    prior_metrics = dataset.get("prior_metrics") or {}

    def ratio_gap(metric: str, actual: Decimal) -> dict[str, Any] | None:
        goal = goals.get(metric)
        if goal is None:
            return None
        target = D(goal["target_value"])
        return {
            "metric": metric,
            "target": str(target.quantize(RATIO_Q)),
            "actual": str(actual.quantize(RATIO_Q)),
            "gap": str((target - actual).quantize(RATIO_Q)),  # 正=尚差
            "achieved": actual >= target,
        }

    growth_gap = None
    if rd_growth is not None and "RD_GROWTH" in goals:
        target = D(goals["RD_GROWTH"]["target_value"])
        growth_gap = {
            "metric": "RD_GROWTH",
            "target": str(target.quantize(RATIO_Q)),
            "actual": str(rd_growth.quantize(RATIO_Q)),
            "gap": str((rd_growth - target).quantize(RATIO_Q)),  # 正=超额
            "achieved": rd_growth >= target,
        }

    return {
        "formula_version": dataset["formula_version"],
        "period_code": dataset["period"]["code"],
        "metrics": {
            "strategic_va_ratio": {
                "gross_value_added": str(q_money(gross_va)),
                "eliminated_value_added": str(q_money(eliminated_va)),
                "net_value_added": str(q_money(net_va)),
                "gross_strategic_value_added": str(q_money(strategic_va)),
                "eliminated_strategic_value_added": str(
                    q_money(eliminated_strategic_va)
                ),
                "net_strategic_value_added": str(q_money(net_strategic_va)),
                "ratio": str(strategic_ratio.quantize(RATIO_Q)),
                "prior_ratio": prior_metrics.get("strategic_va_ratio"),
                "ratio_change": (
                    str((strategic_ratio
                         - D(prior_metrics["strategic_va_ratio"]))
                        .quantize(RATIO_Q))
                    if "strategic_va_ratio" in prior_metrics else None
                ),
                "vs_goal": ratio_gap("STRATEGIC_VA_RATIO", strategic_ratio),
            },
            "basic_research_ratio": {
                "rd_total": str(q_money(rd_total)),
                "basic_total": str(q_money(basic_total)),
                "ratio": str(basic_ratio.quantize(RATIO_Q)),
                "prior_ratio": prior_metrics.get("basic_research_ratio"),
                "ratio_change": (
                    str((basic_ratio
                         - D(prior_metrics["basic_research_ratio"]))
                        .quantize(RATIO_Q))
                    if "basic_research_ratio" in prior_metrics else None
                ),
                "vs_goal": ratio_gap("BASIC_RESEARCH_RATIO", basic_ratio),
            },
            "rd_growth": growth_gap,
            "net_revenue": str(q_money(net_revenue)),
            "public_service_total": str(q_money(public_total)),
        },
        "eliminations": eliminations,
        "projects": _project_drilldown(dataset, attributed, eliminations),
        "approvals": dataset.get("approval_log", []),
        "blockers": [b["message"] for b in find_blockers(dataset)],
    }


def _project_drilldown(
    dataset: dict[str, Any],
    attributed: dict[str, list[dict[str, Any]]],
    eliminations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """项目明细：各方报送毛额、分摊份额与份额后归属、抵销过程。"""
    by_project: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"gross_by_reporter": defaultdict(lambda: defaultdict(Decimal)),
                 "allocated_to_org": defaultdict(lambda: defaultdict(Decimal)),
                 "rules": set(),
                 "eliminations": []}
    )
    for section in ("outputs", "transformations", "rd_expenses"):
        for row in attributed[section]:
            bucket = by_project[row["project_code"]]
            for org, fields in row["gross_by_reporter"].items():
                for field, value in fields.items():
                    bucket["gross_by_reporter"][org][
                        f"{section}.{field}"
                    ] += D(value)
            if row["rule_set_id"]:
                bucket["rules"].add(row["rule_set_id"])
            for allocation in row["allocations"]:
                for field, value in allocation.items():
                    if field in ("value_added", "revenue", "amount"):
                        bucket["allocated_to_org"][allocation["org_code"]][
                            f"{section}.{field}"
                        ] += D(value)
    for elimination in eliminations:
        if elimination.get("project_code"):
            by_project[elimination["project_code"]]["eliminations"].append(elimination)

    result = []
    for code, bucket in sorted(by_project.items()):
        result.append(
            {
                "project_code": code,
                "project_name": dataset["projects_by_code"][code]["name"],
                "strategic": _project_strategic(dataset, code),
                "rule_set_ids": sorted(bucket["rules"]),
                "gross_by_reporter": {
                    org: {k: str(q_money(v)) for k, v in fields.items()}
                    for org, fields in bucket["gross_by_reporter"].items()
                },
                "allocated_to_org": {
                    org: {k: str(q_money(v)) for k, v in fields.items()}
                    for org, fields in bucket["allocated_to_org"].items()
                },
                "eliminations": bucket["eliminations"],
            }
        )
    return result


# --------------------------------------------------------------------------
# 快照序列化
# --------------------------------------------------------------------------
def to_jsonable(dataset_or_report: Any) -> str:
    """把含 Decimal 的结构序列化为快照文本。"""

    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"不可序列化的类型：{type(obj)!r}")

    return json.dumps(dataset_or_report, ensure_ascii=False, default=default,
                      sort_keys=True)


def dataset_fingerprint(payload: str) -> str:
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
