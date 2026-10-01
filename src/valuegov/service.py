"""价值贡献治理核心服务。

统一维护组织边界、产业分类、项目任务、投资批次、研发费用、基础研究
属性、成果转化、公共服务贡献与内部交易抵销；并负责：

- 报告期关账：公式与组织范围随快照冻结，关账由可恢复作业执行；
- 重组、项目拆分与跨单位协作：按可审计的分配规则归属贡献；
- 内部交易：抵销后只保留净影响，抵销过程全程可查；
- 证据与审批：业务单位提交证据，特殊归类不得自行批准；
- 规划目标：只能影响开放期间，历史错误一律通过更正版本处理；
- 批量导入：识别重送，冲突材料进入复核队列；
- 后台作业：未完成的关账与证据催补在进程重启后继续。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .domain import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    ROLE_SUBMITTER,
    canonical,
    new_id,
    now_iso,
    parse_instant,
    payload_hash,
    prior_period,
    to_minor,
    validate_period,
)
from .errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    PeriodClosedError,
    ValidationError,
)
from .metrics import compute_figures, evaluate, validate_formula
from .storage import Store

SUBMIT_ROLES = (ROLE_SUBMITTER, ROLE_ADMIN)
REVIEW_ROLES = (ROLE_REVIEWER, ROLE_ADMIN)
PLAN_ROLES = (ROLE_PLANNER, ROLE_ADMIN)

UNIT_TYPES = frozenset({"GROUP", "SEGMENT", "INSTITUTE", "SERVICE", "ENTERPRISE"})
EXPENSE_KINDS = frozenset({"PERSONNEL", "MATERIAL", "EQUIPMENT", "OUTSOURCING", "SERVICE", "OTHER"})

EVIDENCE_SE_CLASSIFICATION = "SE_CLASSIFICATION"
EVIDENCE_BASIC_RESEARCH = "BASIC_RESEARCH_CLAIM"

JOB_PERIOD_CLOSE = "PERIOD_CLOSE"
JOB_REMINDER_SWEEP = "REMINDER_SWEEP"

DEFAULT_FORMULA = {
    "metrics": {
        "se_value_added_ratio": {
            "kind": "ratio",
            "numerator": {"source": "transformation_value_added", "se_only": True, "eliminate": True},
            "denominator": {"source": "transformation_value_added", "eliminate": True},
        },
        "basic_research_ratio": {
            "kind": "ratio",
            "numerator": {"source": "rd_expense", "basic_only": True},
            "denominator": {"source": "rd_expense"},
        },
        "rd_expense_total": {"kind": "sum", "source": "rd_expense"},
        "rd_expense_growth": {"kind": "growth", "source": "rd_expense"},
        "public_service_value": {"kind": "sum", "source": "public_service_value"},
        "revenue_total": {"kind": "sum", "source": "revenue", "eliminate": True},
        "investment_total": {"kind": "sum", "source": "investment"},
    }
}

# 可更正实体：允许修改的字段（对外字段名 -> 列名），金额字段在写入前换算。
CORRECTION_TARGETS = {
    "rd_expense": {
        "table": "rd_expenses",
        "pk": "expense_id",
        "prefix": "EXP",
        "fields": {
            "amount": "amount_minor",
            "expense_kind": "expense_kind",
            "is_basic_research": "is_basic_research",
            "basic_research_basis": "basic_research_basis",
            "void": "void",
        },
        "money": {"amount"},
    },
    "transformation": {
        "table": "transformations",
        "pk": "transformation_id",
        "prefix": "TRN",
        "fields": {
            "revenue": "revenue_minor",
            "value_added": "value_added_minor",
            "description": "description",
            "void": "void",
        },
        "money": {"revenue", "value_added"},
    },
    "public_service": {
        "table": "public_service_contributions",
        "pk": "contribution_id",
        "prefix": "PSC",
        "fields": {
            "value": "value_minor",
            "service_type": "service_type",
            "beneficiary_scope": "beneficiary_scope",
            "description": "description",
            "void": "void",
        },
        "money": {"value"},
    },
    "investment_batch": {
        "table": "investment_batches",
        "pk": "batch_id",
        "prefix": "INV",
        "fields": {
            "amount": "amount_minor",
            "funding_source": "funding_source",
            "note": "note",
            "void": "void",
        },
        "money": {"amount"},
    },
    "internal_transaction": {
        "table": "internal_transactions",
        "pk": "txn_id",
        "prefix": "ITX",
        "fields": {
            "amount": "amount_minor",
            "value_added": "value_added_minor",
            "description": "description",
            "void": "void",
        },
        "money": {"amount", "value_added"},
    },
}

IMPORT_TYPES = frozenset({"EXPENSE", "TRANSFORMATION", "PUBLIC_SERVICE", "INVESTMENT", "INTERNAL_TXN"})


class ValueGovService:
    """价值贡献治理后端门面，所有用例都经过它进入领域。"""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        clock=None,
        evidence_sla_hours: float = 24 * 7,
        admin: str = "system-admin",
        auto_recover: bool = True,
    ) -> None:
        self.store = Store(db_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evidence_sla = timedelta(hours=evidence_sla_hours)
        self.admin = admin
        self._job_handlers = {
            JOB_PERIOD_CLOSE: self._job_close_period,
            JOB_REMINDER_SWEEP: self._job_reminder_sweep,
        }
        with self.store.tx():
            self._ensure_principal(admin, [ROLE_ADMIN, ROLE_REVIEWER, ROLE_PLANNER, ROLE_SUBMITTER])
            if not self.store.q1("SELECT version FROM formula_versions ORDER BY version DESC LIMIT 1"):
                self._insert_formula(self.admin, DEFAULT_FORMULA)
        if auto_recover:
            self.recover()

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return now_iso(self.clock)

    def _ensure_principal(self, name: str, roles: list) -> None:
        if not self.store.q1("SELECT name FROM principals WHERE name=?", (name,)):
            self.store.run(
                "INSERT INTO principals(name, roles) VALUES(?, ?)",
                (name, json.dumps(sorted(roles), ensure_ascii=False)),
            )

    def _principal_roles(self, actor: str) -> set:
        row = self.store.q1("SELECT roles FROM principals WHERE name=?", (actor,))
        if not row:
            raise ForbiddenError(f"未登记的操作者: {actor}")
        return set(json.loads(row["roles"]))

    def _require(self, actor: str, *roles: str) -> None:
        if not self._principal_roles(actor).intersection(roles):
            raise ForbiddenError(f"操作者 {actor} 缺少角色 {'/'.join(roles)}")

    def _audit(self, actor: str, action: str, entity_type: str, entity_id: str, detail) -> None:
        self.store.run(
            "INSERT INTO audit_log(actor, action, entity_type, entity_id, detail, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (actor, action, entity_type, entity_id, canonical(detail), self._now()),
        )

    def _unit_by_code(self, code: str):
        row = self.store.q1("SELECT * FROM org_units WHERE code=?", (code,))
        if not row:
            raise NotFoundError(f"组织单位不存在: {code}")
        return row

    def _project_by_code(self, code: str):
        row = self.store.q1("SELECT * FROM projects WHERE code=?", (code,))
        if not row:
            raise NotFoundError(f"项目不存在: {code}")
        return row

    def _category_by_code(self, code: str):
        row = self.store.q1("SELECT * FROM industry_categories WHERE code=?", (code,))
        if not row:
            raise NotFoundError(f"产业分类不存在: {code}")
        return row

    def _period_row(self, period: str):
        return self.store.q1("SELECT * FROM periods WHERE period=?", (period,))

    def _require_period_open(self, period: str) -> None:
        row = self._period_row(period)
        if not row:
            raise NotFoundError(f"报告期不存在: {period}")
        if row["status"] != "OPEN":
            raise PeriodClosedError(f"报告期 {period} 已关账，历史错误请通过更正版本处理")

    def _require_period_mutable(self, period: str) -> None:
        """期间不存在（未来）或开放时才允许修改。"""
        row = self._period_row(period)
        if row and row["status"] != "OPEN":
            raise PeriodClosedError(f"报告期 {period} 已关账，只能影响开放期间")

    def _require_unit_bookable(self, unit, period: str) -> None:
        if unit["status"] == "RETIRED" and unit["retired_from_period"] and unit["retired_from_period"] <= period:
            raise ConflictError(f"单位 {unit['code']} 自 {unit['retired_from_period']} 起已因重组退出，请使用承继单位")

    def _require_project_bookable(self, project, period: str) -> None:
        if project["status"] == "SPLIT" and project["split_from_period"] and project["split_from_period"] <= period:
            raise ConflictError(f"项目 {project['code']} 自 {project['split_from_period']} 起已拆分，请使用子项目")
        if project["status"] == "CLOSED":
            raise ConflictError(f"项目 {project['code']} 已终止")

    def _org_scope(self, period: str) -> set:
        rows = self.store.qa("SELECT unit_id, status, retired_from_period FROM org_units")
        return {
            r["unit_id"]
            for r in rows
            if r["status"] == "ACTIVE" or (r["retired_from_period"] and r["retired_from_period"] > period)
        }

    def _unit_map(self, period: str) -> dict:
        events = self.store.qa(
            "SELECT from_unit_id, to_unit_id FROM reorg_events WHERE effective_period<=? "
            "ORDER BY effective_period, created_at",
            (period,),
        )
        mapping = {e["from_unit_id"]: e["to_unit_id"] for e in events}

        def resolve(unit_id: str) -> str:
            seen = set()
            while unit_id in mapping and unit_id not in seen:
                seen.add(unit_id)
                unit_id = mapping[unit_id]
            return unit_id

        return {e["from_unit_id"]: resolve(e["from_unit_id"]) for e in events}

    def _latest_formula(self) -> tuple:
        row = self.store.q1("SELECT * FROM formula_versions ORDER BY version DESC LIMIT 1")
        return row["version"], json.loads(row["definition"])

    def _figures_for(self, period: str) -> dict:
        return compute_figures(self.store, period, self._org_scope(period), self._unit_map(period))

    def _metrics_for(self, period: str, definition: dict) -> tuple:
        figures = self._figures_for(period)
        previous = prior_period(period)
        prior_figures = None
        if previous and self._period_row(previous):
            prior_figures = self._figures_for(previous)
        return evaluate(definition, figures, prior_figures), figures

    def _active_targets(self, period: str) -> dict:
        rows = self.store.qa(
            "SELECT metric, target_value FROM plan_targets WHERE period=? AND superseded_by IS NULL",
            (period,),
        )
        return {r["metric"]: r["target_value"] for r in rows}

    # ------------------------------------------------------------------
    # 主数据：操作者、组织边界、重组、产业分类、公式、报告期、规划目标
    # ------------------------------------------------------------------

    def register_principal(self, actor: str, name: str, roles: list) -> dict:
        self._require(actor, ROLE_ADMIN)
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("操作者名称不能为空")
        role_set = set(roles or [])
        if not role_set or not role_set <= ALL_ROLES:
            raise ValidationError(f"角色无效，允许值: {sorted(ALL_ROLES)}")
        with self.store.tx():
            self.store.run(
                "INSERT INTO principals(name, roles) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET roles=excluded.roles",
                (name, json.dumps(sorted(role_set), ensure_ascii=False)),
            )
            self._audit(actor, "PRINCIPAL_UPSERT", "principal", name, {"roles": sorted(role_set)})
        return {"name": name, "roles": sorted(role_set)}

    def create_unit(self, actor: str, code: str, name: str, unit_type: str, parent_code: str | None = None) -> dict:
        self._require(actor, ROLE_ADMIN)
        if unit_type not in UNIT_TYPES:
            raise ValidationError(f"单位类型无效，允许值: {sorted(UNIT_TYPES)}")
        if not code.strip() or not name.strip():
            raise ValidationError("单位编码与名称不能为空")
        parent_id = self._unit_by_code(parent_code)["unit_id"] if parent_code else None
        if self.store.q1("SELECT 1 FROM org_units WHERE code=?", (code,)):
            raise ConflictError(f"单位编码已存在: {code}")
        unit_id = new_id("UNT")
        with self.store.tx():
            self.store.run(
                "INSERT INTO org_units(unit_id, code, name, unit_type, parent_id, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (unit_id, code, name, unit_type, parent_id, actor, self._now()),
            )
            self._audit(actor, "UNIT_CREATE", "org_unit", unit_id, {"code": code, "unit_type": unit_type})
        return {"unit_id": unit_id, "code": code}

    def list_units(self) -> list:
        return [dict(r) for r in self.store.qa("SELECT * FROM org_units ORDER BY code")]

    def register_reorg(self, actor: str, from_code: str, to_code: str, effective_period: str, reason: str) -> dict:
        """登记重组：自生效报告期起，原单位的贡献归属承继单位。"""
        self._require(actor, ROLE_ADMIN)
        validate_period(effective_period)
        if not reason.strip():
            raise ValidationError("重组原因不能为空")
        source = self._unit_by_code(from_code)
        target = self._unit_by_code(to_code)
        if source["unit_id"] == target["unit_id"]:
            raise ValidationError("重组前后单位不能相同")
        if source["status"] == "RETIRED":
            raise ConflictError(f"单位 {from_code} 已处于退出状态")
        self._require_period_mutable(effective_period)
        self._require_unit_bookable(target, effective_period)
        event_id = new_id("ORG")
        with self.store.tx():
            self.store.run(
                "INSERT INTO reorg_events(event_id, from_unit_id, to_unit_id, effective_period, reason, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (event_id, source["unit_id"], target["unit_id"], effective_period, reason, actor, self._now()),
            )
            self.store.run(
                "UPDATE org_units SET status='RETIRED', retired_from_period=? WHERE unit_id=?",
                (effective_period, source["unit_id"]),
            )
            self._audit(
                actor, "UNIT_REORG", "org_unit", source["unit_id"],
                {"to": target["code"], "effective_period": effective_period, "reason": reason},
            )
        return {"event_id": event_id}

    def create_category(
        self, actor: str, code: str, name: str, is_strategic_emerging: bool = False, parent_code: str | None = None
    ) -> dict:
        self._require(actor, ROLE_ADMIN)
        if not code.strip() or not name.strip():
            raise ValidationError("分类编码与名称不能为空")
        if parent_code:
            self._category_by_code(parent_code)
        if self.store.q1("SELECT 1 FROM industry_categories WHERE code=?", (code,)):
            raise ConflictError(f"分类编码已存在: {code}")
        category_id = new_id("CAT")
        with self.store.tx():
            self.store.run(
                "INSERT INTO industry_categories(category_id, code, name, is_strategic_emerging, parent_code, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (category_id, code, name, 1 if is_strategic_emerging else 0, parent_code, actor, self._now()),
            )
            self._audit(actor, "CATEGORY_CREATE", "industry_category", category_id, {"code": code})
        return {"category_id": category_id, "code": code}

    def list_categories(self) -> list:
        return [dict(r) for r in self.store.qa("SELECT * FROM industry_categories ORDER BY code")]

    def _insert_formula(self, actor: str, definition: dict) -> int:
        validate_formula(definition)
        row = self.store.q1("SELECT MAX(version) AS v FROM formula_versions")
        version = (row["v"] or 0) + 1
        self.store.run(
            "INSERT INTO formula_versions(version, definition, created_by, created_at) VALUES(?, ?, ?, ?)",
            (version, canonical(definition), actor, self._now()),
        )
        return version

    def publish_formula(self, actor: str, definition: dict) -> dict:
        self._require(actor, ROLE_ADMIN)
        with self.store.tx():
            version = self._insert_formula(actor, definition)
            self._audit(actor, "FORMULA_PUBLISH", "formula_version", str(version), {"metrics": sorted(definition["metrics"])})
        return {"version": version}

    def open_period(self, actor: str, period: str) -> dict:
        self._require(actor, *PLAN_ROLES)
        validate_period(period)
        if self._period_row(period):
            raise ConflictError(f"报告期已存在: {period}")
        with self.store.tx():
            self.store.run(
                "INSERT INTO periods(period, status, opened_at) VALUES(?, 'OPEN', ?)",
                (period, self._now()),
            )
            self._audit(actor, "PERIOD_OPEN", "period", period, {})
        return {"period": period, "status": "OPEN"}

    def get_period(self, period: str) -> dict:
        row = self._period_row(period)
        if not row:
            raise NotFoundError(f"报告期不存在: {period}")
        return dict(row)

    def list_periods(self) -> list:
        return [dict(r) for r in self.store.qa("SELECT * FROM periods ORDER BY period")]

    def set_target(self, actor: str, metric: str, period: str, value: float) -> dict:
        """设置规划目标；已关账期间的目标随快照冻结，只能影响开放期间。"""
        self._require(actor, *PLAN_ROLES)
        validate_period(period)
        _version, definition = self._latest_formula()
        if metric not in definition["metrics"]:
            raise ValidationError(f"未知指标: {metric}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("目标值必须是数值")
        kind = definition["metrics"][metric]["kind"]
        if kind == "ratio" and not 0 <= value <= 1:
            raise ValidationError("比率类目标必须在 0 与 1 之间")
        if kind != "ratio" and value < 0:
            raise ValidationError("目标值不能为负")
        self._require_period_mutable(period)
        target_id = new_id("TGT")
        with self.store.tx():
            self.store.run(
                "UPDATE plan_targets SET superseded_by=? WHERE metric=? AND period=? AND superseded_by IS NULL",
                (target_id, metric, period),
            )
            self.store.run(
                "INSERT INTO plan_targets(target_id, metric, period, target_value, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (target_id, metric, period, float(value), actor, self._now()),
            )
            self._audit(actor, "TARGET_SET", "plan_target", target_id, {"metric": metric, "period": period, "value": value})
        return {"target_id": target_id}

    def list_targets(self, period: str | None = None) -> list:
        if period:
            rows = self.store.qa("SELECT * FROM plan_targets WHERE period=? ORDER BY metric, created_at", (period,))
        else:
            rows = self.store.qa("SELECT * FROM plan_targets ORDER BY period, metric, created_at")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 项目任务、特殊归类、分配规则、项目拆分、投资批次
    # ------------------------------------------------------------------

    def create_project(
        self, actor: str, code: str, name: str, owner_unit_code: str, category_code: str | None = None
    ) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        if not code.strip() or not name.strip():
            raise ValidationError("项目编码与名称不能为空")
        owner = self._unit_by_code(owner_unit_code)
        if owner["status"] == "RETIRED":
            raise ConflictError(f"单位 {owner_unit_code} 已退出，不能承接新项目")
        if self.store.q1("SELECT 1 FROM projects WHERE code=?", (code,)):
            raise ConflictError(f"项目编码已存在: {code}")
        category_id = None
        pending_category_id = None
        evidence_id = None
        if category_code:
            category = self._category_by_code(category_code)
            if category["is_strategic_emerging"]:
                # 战略性新兴产业归类属于特殊归类，必须经复核批准后才生效。
                pending_category_id = category["category_id"]
            else:
                category_id = category["category_id"]
        project_id = new_id("PRJ")
        with self.store.tx():
            self.store.run(
                "INSERT INTO projects(project_id, code, name, owner_unit_id, category_id, pending_category_id, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, code, name, owner["unit_id"], category_id, pending_category_id, actor, self._now()),
            )
            if pending_category_id:
                evidence_id = self._insert_evidence(
                    actor,
                    EVIDENCE_SE_CLASSIFICATION,
                    project_id,
                    None,
                    {"project_id": project_id, "category_id": pending_category_id, "category_code": category_code,
                     "justification": "创建项目时申请战新归类"},
                )
            self._audit(actor, "PROJECT_CREATE", "project", project_id, {"code": code, "owner": owner_unit_code})
        result = {"project_id": project_id, "code": code}
        if evidence_id:
            result["classification_evidence_id"] = evidence_id
        return result

    def get_project(self, code: str) -> dict:
        project = self._project_by_code(code)
        result = dict(project)
        for key, table in (("category_id", "category"), ("pending_category_id", "pending_category")):
            if project[key]:
                row = self.store.q1("SELECT code FROM industry_categories WHERE category_id=?", (project[key],))
                result[f"{table}_code"] = row["code"] if row else None
        owner = self.store.q1("SELECT code FROM org_units WHERE unit_id=?", (project["owner_unit_id"],))
        result["owner_unit_code"] = owner["code"] if owner else None
        return result

    def request_se_classification(self, actor: str, project_code: str, category_code: str, justification: str) -> dict:
        """申请把项目归入战略性新兴产业分类（特殊归类，需复核批准）。"""
        self._require(actor, *SUBMIT_ROLES)
        project = self._project_by_code(project_code)
        category = self._category_by_code(category_code)
        if not category["is_strategic_emerging"]:
            raise ValidationError("只有战略性新兴产业分类需要走归类审批")
        if project["category_id"] == category["category_id"]:
            raise ConflictError("项目已属于该分类")
        if project["pending_category_id"]:
            raise ConflictError("该项目存在待审批的归类申请")
        if not justification.strip():
            raise ValidationError("归类依据不能为空")
        with self.store.tx():
            self.store.run(
                "UPDATE projects SET pending_category_id=? WHERE project_id=?",
                (category["category_id"], project["project_id"]),
            )
            evidence_id = self._insert_evidence(
                actor,
                EVIDENCE_SE_CLASSIFICATION,
                project["project_id"],
                None,
                {"project_id": project["project_id"], "category_id": category["category_id"],
                 "category_code": category_code, "justification": justification},
            )
            self._audit(actor, "SE_CLASSIFICATION_REQUEST", "project", project["project_id"], {"category": category_code})
        return {"evidence_id": evidence_id}

    def set_allocation(self, actor: str, project_code: str, period: str, shares: list, basis: str) -> dict:
        """为联合项目登记跨单位分配规则；同一期间的权重合计必须等于 1。"""
        self._require(actor, *SUBMIT_ROLES)
        project = self._project_by_code(project_code)
        validate_period(period)
        self._require_period_mutable(period)
        if not isinstance(shares, list) or not shares:
            raise ValidationError("分配份额不能为空")
        if not basis.strip():
            raise ValidationError("分配依据不能为空")
        total = 0.0
        resolved = []
        seen_units = set()
        for share in shares:
            if not isinstance(share, dict):
                raise ValidationError("分配份额必须是对象")
            unit = self._unit_by_code(share.get("unit_code", ""))
            weight = share.get("weight")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 1:
                raise ValidationError("分配权重必须是 (0, 1] 区间内的数值")
            if unit["unit_id"] in seen_units:
                raise ValidationError(f"单位 {unit['code']} 的份额重复")
            seen_units.add(unit["unit_id"])
            self._require_unit_bookable(unit, period)
            resolved.append((unit["unit_id"], float(weight)))
            total += float(weight)
        if abs(total - 1.0) > 1e-6:
            raise ValidationError(f"同一期间的分配权重合计必须等于 1，当前为 {total}")
        rule_set_id = new_id("ARS")
        with self.store.tx():
            self.store.run(
                "UPDATE allocation_rules SET superseded_by=? "
                "WHERE project_id=? AND effective_period=? AND superseded_by IS NULL",
                (rule_set_id, project["project_id"], period),
            )
            for unit_id, weight in resolved:
                self.store.run(
                    "INSERT INTO allocation_rules(rule_id, rule_set_id, project_id, unit_id, weight, basis, "
                    "effective_period, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_id("ALR"), rule_set_id, project["project_id"], unit_id, weight, basis, period,
                     actor, self._now()),
                )
            self._audit(
                actor, "ALLOCATION_SET", "project", project["project_id"],
                {"period": period, "rule_set_id": rule_set_id,
                 "shares": [{"unit_id": u, "weight": w} for u, w in resolved], "basis": basis},
            )
        return {"rule_set_id": rule_set_id}

    def split_project(self, actor: str, project_code: str, period: str, children: list, reason: str) -> dict:
        """把项目拆分为若干子项目；子项目按权重继承当期的协作分配规则。"""
        self._require(actor, ROLE_ADMIN, ROLE_PLANNER)
        parent = self._project_by_code(project_code)
        validate_period(period)
        self._require_period_mutable(period)
        if parent["status"] != "ACTIVE":
            raise ConflictError(f"项目 {project_code} 当前状态不允许拆分")
        if not isinstance(children, list) or len(children) < 2:
            raise ValidationError("拆分至少需要两个子项目")
        if not reason.strip():
            raise ValidationError("拆分原因不能为空")
        total = 0.0
        specs = []
        for child in children:
            if not isinstance(child, dict):
                raise ValidationError("子项目必须是对象")
            code = child.get("code", "")
            name = child.get("name", "")
            weight = child.get("weight")
            if self.store.q1("SELECT 1 FROM projects WHERE code=?", (code,)):
                raise ConflictError(f"项目编码已存在: {code}")
            owner = self._unit_by_code(child.get("owner_unit_code", ""))
            self._require_unit_bookable(owner, period)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight < 1:
                raise ValidationError("子项目权重必须是 (0, 1) 区间内的数值")
            specs.append((code, name, owner["unit_id"], float(weight)))
            total += float(weight)
        if abs(total - 1.0) > 1e-6:
            raise ValidationError(f"子项目权重合计必须等于 1，当前为 {total}")
        split_group_id = new_id("SPL")
        created = []
        with self.store.tx():
            parent_rules = self.store.qa(
                "SELECT unit_id, weight, basis FROM allocation_rules "
                "WHERE project_id=? AND effective_period=? AND superseded_by IS NULL",
                (parent["project_id"], period),
            )
            if not parent_rules:
                parent_rules = [{"unit_id": parent["owner_unit_id"], "weight": 1.0, "basis": "业主单位默认"}]
            for code, name, owner_id, weight in specs:
                child_id = new_id("PRJ")
                self.store.run(
                    "INSERT INTO projects(project_id, code, name, owner_unit_id, category_id, parent_project_id, "
                    "split_group_id, split_weight, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (child_id, code, name, owner_id, parent["category_id"], parent["project_id"],
                     split_group_id, weight, actor, self._now()),
                )
                for rule in parent_rules:
                    # 子项目沿用父项目的协作分配结构（权重和仍为 1），
                    # 子项目自身的拆分权重仅作为审计元数据保留。
                    self.store.run(
                        "INSERT INTO allocation_rules(rule_id, rule_set_id, project_id, unit_id, weight, basis, "
                        "effective_period, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (new_id("ALR"), split_group_id, child_id, rule["unit_id"], rule["weight"],
                         f"拆分继承：{reason}", period, actor, self._now()),
                    )
                created.append({"project_id": child_id, "code": code, "weight": weight})
            self.store.run(
                "UPDATE projects SET status='SPLIT', split_from_period=? WHERE project_id=?",
                (period, parent["project_id"]),
            )
            self._audit(
                actor, "PROJECT_SPLIT", "project", parent["project_id"],
                {"period": period, "reason": reason, "children": created},
            )
        return {"split_group_id": split_group_id, "children": created}

    def record_investment(
        self, actor: str, project_code: str, period: str, amount, funding_source: str, note: str = ""
    ) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        validate_period(period)
        self._require_period_open(period)
        project = self._project_by_code(project_code)
        self._require_project_bookable(project, period)
        amount_minor = to_minor(amount)
        if amount_minor <= 0:
            raise ValidationError("投资金额必须为正")
        if not funding_source.strip():
            raise ValidationError("资金来源不能为空")
        batch_id = new_id("INV")
        with self.store.tx():
            self.store.run(
                "INSERT INTO investment_batches(batch_id, project_id, period, amount_minor, funding_source, note, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, project["project_id"], period, amount_minor, funding_source, note, actor, self._now()),
            )
            self._audit(actor, "INVESTMENT_RECORD", "investment_batch", batch_id,
                        {"project": project_code, "period": period, "amount": amount})
        return {"batch_id": batch_id}

    # ------------------------------------------------------------------
    # 研发费用、基础研究属性、成果转化、公共服务贡献
    # ------------------------------------------------------------------

    def record_expense(self, actor: str, project_code: str, period: str, amount, kind: str) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        validate_period(period)
        self._require_period_open(period)
        project = self._project_by_code(project_code)
        self._require_project_bookable(project, period)
        amount_minor = to_minor(amount)
        if amount_minor <= 0:
            raise ValidationError("研发费用金额必须为正")
        if kind not in EXPENSE_KINDS:
            raise ValidationError(f"费用类别无效，允许值: {sorted(EXPENSE_KINDS)}")
        expense_id = new_id("EXP")
        with self.store.tx():
            self.store.run(
                "INSERT INTO rd_expenses(expense_id, project_id, period, amount_minor, expense_kind, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (expense_id, project["project_id"], period, amount_minor, kind, actor, self._now()),
            )
            self._audit(actor, "EXPENSE_RECORD", "rd_expense", expense_id,
                        {"project": project_code, "period": period, "amount": amount, "kind": kind})
        return {"expense_id": expense_id}

    def claim_basic_research(self, actor: str, expense_id: str, basis: str) -> dict:
        """申报研发费用属于基础研究；特殊归类，需复核批准后生效。"""
        self._require(actor, *SUBMIT_ROLES)
        expense = self.store.q1("SELECT * FROM rd_expenses WHERE expense_id=?", (expense_id,))
        if not expense or expense["superseded_by"] or expense["void"]:
            raise NotFoundError(f"费用记录不存在或已失效: {expense_id}")
        if expense["is_basic_research"]:
            raise ConflictError("该费用已认定为基础研究")
        if expense["pending_basic_research"]:
            raise ConflictError("该费用存在待审批的基础研究申报")
        if not basis.strip():
            raise ValidationError("基础研究认定依据不能为空")
        period_row = self._period_row(expense["period"])
        if period_row and period_row["status"] != "OPEN":
            raise PeriodClosedError(f"报告期 {expense['period']} 已关账，请通过更正版本调整基础研究属性")
        with self.store.tx():
            self.store.run(
                "UPDATE rd_expenses SET pending_basic_research=1 WHERE expense_id=?", (expense_id,)
            )
            evidence_id = self._insert_evidence(
                actor, EVIDENCE_BASIC_RESEARCH, expense_id, expense["period"],
                {"expense_id": expense_id, "basis": basis},
            )
            self._audit(actor, "BASIC_RESEARCH_CLAIM", "rd_expense", expense_id, {"basis": basis})
        return {"evidence_id": evidence_id}

    def record_transformation(
        self, actor: str, project_code: str, period: str, revenue, value_added, description: str = ""
    ) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        validate_period(period)
        self._require_period_open(period)
        project = self._project_by_code(project_code)
        self._require_project_bookable(project, period)
        revenue_minor = to_minor(revenue)
        value_added_minor = to_minor(value_added)
        if revenue_minor < 0 or value_added_minor < 0 or (revenue_minor == 0 and value_added_minor == 0):
            raise ValidationError("转化收入与增加值不能为负，且至少一项为正")
        transformation_id = new_id("TRN")
        with self.store.tx():
            self.store.run(
                "INSERT INTO transformations(transformation_id, project_id, period, revenue_minor, "
                "value_added_minor, description, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (transformation_id, project["project_id"], period, revenue_minor, value_added_minor,
                 description, actor, self._now()),
            )
            self._audit(actor, "TRANSFORMATION_RECORD", "transformation", transformation_id,
                        {"project": project_code, "period": period})
        return {"transformation_id": transformation_id}

    def record_public_service(
        self, actor: str, unit_code: str, period: str, service_type: str, value,
        beneficiary_scope: str, description: str = "",
    ) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        validate_period(period)
        self._require_period_open(period)
        unit = self._unit_by_code(unit_code)
        self._require_unit_bookable(unit, period)
        value_minor = to_minor(value)
        if value_minor <= 0:
            raise ValidationError("公共服务贡献价值必须为正")
        if not service_type.strip() or not beneficiary_scope.strip():
            raise ValidationError("服务类型与受益范围不能为空")
        contribution_id = new_id("PSC")
        with self.store.tx():
            self.store.run(
                "INSERT INTO public_service_contributions(contribution_id, unit_id, period, service_type, "
                "value_minor, beneficiary_scope, description, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (contribution_id, unit["unit_id"], period, service_type, value_minor, beneficiary_scope,
                 description, actor, self._now()),
            )
            self._audit(actor, "PUBLIC_SERVICE_RECORD", "public_service_contribution", contribution_id,
                        {"unit": unit_code, "period": period})
        return {"contribution_id": contribution_id}

    # ------------------------------------------------------------------
    # 内部交易与抵销
    # ------------------------------------------------------------------

    def record_internal_transaction(
        self, actor: str, seller_unit_code: str, buyer_unit_code: str, period: str, amount,
        value_added=0, project_code: str | None = None, description: str = "",
    ) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        validate_period(period)
        self._require_period_open(period)
        seller = self._unit_by_code(seller_unit_code)
        buyer = self._unit_by_code(buyer_unit_code)
        if seller["unit_id"] == buyer["unit_id"]:
            raise ValidationError("内部交易双方不能是同一单位")
        self._require_unit_bookable(seller, period)
        self._require_unit_bookable(buyer, period)
        amount_minor = to_minor(amount)
        value_added_minor = to_minor(value_added)
        if amount_minor <= 0:
            raise ValidationError("内部交易金额必须为正")
        if not 0 <= value_added_minor <= amount_minor:
            raise ValidationError("内部交易包含的增加值必须介于 0 与交易金额之间")
        project_id = self._project_by_code(project_code)["project_id"] if project_code else None
        txn_id = new_id("ITX")
        with self.store.tx():
            self.store.run(
                "INSERT INTO internal_transactions(txn_id, period, seller_unit_id, buyer_unit_id, project_id, "
                "amount_minor, value_added_minor, description, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (txn_id, period, seller["unit_id"], buyer["unit_id"], project_id, amount_minor,
                 value_added_minor, description, actor, self._now()),
            )
            self._audit(actor, "INTERNAL_TXN_RECORD", "internal_transaction", txn_id,
                        {"seller": seller_unit_code, "buyer": buyer_unit_code, "period": period})
        return {"txn_id": txn_id}

    def run_elimination(self, actor: str, period: str) -> dict:
        """对报告期全部未抵销的内部交易执行抵销；只保留抵销后的净影响。"""
        self._require(actor, *REVIEW_ROLES)
        validate_period(period)
        self._require_period_open(period)
        txns = self.store.qa(
            "SELECT * FROM internal_transactions WHERE period=? AND status='OPEN' "
            "AND superseded_by IS NULL AND void=0 ORDER BY created_at",
            (period,),
        )
        if not txns:
            return {"run_id": None, "eliminated": 0, "amount": 0.0, "value_added": 0.0}
        run_id = new_id("ELR")
        amount_total = sum(t["amount_minor"] for t in txns)
        value_added_total = sum(t["value_added_minor"] for t in txns)
        with self.store.tx():
            self.store.run(
                "INSERT INTO elimination_runs(run_id, period, created_by, created_at) VALUES(?, ?, ?, ?)",
                (run_id, period, actor, self._now()),
            )
            for txn in txns:
                self.store.run(
                    "INSERT INTO elimination_entries(entry_id, run_id, txn_id, amount_minor, value_added_minor) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (new_id("ELE"), run_id, txn["txn_id"], txn["amount_minor"], txn["value_added_minor"]),
                )
                self.store.run(
                    "UPDATE internal_transactions SET status='ELIMINATED' WHERE txn_id=?", (txn["txn_id"],)
                )
            self._audit(actor, "ELIMINATION_RUN", "elimination_run", run_id,
                        {"period": period, "eliminated": len(txns)})
        return {
            "run_id": run_id,
            "eliminated": len(txns),
            "amount": amount_total / 100,
            "value_added": value_added_total / 100,
        }

    # ------------------------------------------------------------------
    # 证据与审批：提交人不得自行批准
    # ------------------------------------------------------------------

    def _insert_evidence(self, actor: str, subject_type: str, subject_id: str, period, payload: dict) -> str:
        evidence_id = new_id("EVD")
        self.store.run(
            "INSERT INTO evidence(evidence_id, subject_type, subject_id, period, payload, submitted_by, "
            "submitted_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, subject_type, subject_id, period, canonical(payload), actor, self._now()),
        )
        return evidence_id

    def list_evidence(self, status: str | None = None) -> list:
        if status:
            rows = self.store.qa("SELECT * FROM evidence WHERE status=? ORDER BY submitted_at", (status,))
        else:
            rows = self.store.qa("SELECT * FROM evidence ORDER BY submitted_at")
        return [dict(r) for r in rows]

    def _evidence_row(self, evidence_id: str):
        row = self.store.q1("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
        if not row:
            raise NotFoundError(f"证据不存在: {evidence_id}")
        return row

    def _check_decider(self, actor: str, submitted_by: str) -> None:
        self._require(actor, *REVIEW_ROLES)
        if actor == submitted_by:
            raise ForbiddenError("提交人不得自行批准或驳回自己提交的材料")

    def approve_evidence(self, actor: str, evidence_id: str, note: str | None = None) -> dict:
        evidence = self._evidence_row(evidence_id)
        self._check_decider(actor, evidence["submitted_by"])
        if evidence["status"] != "PENDING":
            raise ConflictError(f"证据当前状态为 {evidence['status']}，不能重复审批")
        payload = json.loads(evidence["payload"])
        with self.store.tx():
            if evidence["subject_type"] == EVIDENCE_SE_CLASSIFICATION:
                self.store.run(
                    "UPDATE projects SET category_id=?, pending_category_id=NULL WHERE project_id=?",
                    (payload["category_id"], evidence["subject_id"]),
                )
            elif evidence["subject_type"] == EVIDENCE_BASIC_RESEARCH:
                self.store.run(
                    "UPDATE rd_expenses SET is_basic_research=1, basic_research_basis=?, "
                    "pending_basic_research=0 WHERE expense_id=?",
                    (payload["basis"], evidence["subject_id"]),
                )
            else:  # pragma: no cover - 防御未知类型
                raise ValidationError(f"未知证据类型: {evidence['subject_type']}")
            self.store.run(
                "UPDATE evidence SET status='APPROVED', decided_by=?, decided_at=?, decision_note=? "
                "WHERE evidence_id=?",
                (actor, self._now(), note, evidence_id),
            )
            self.store.run(
                "UPDATE evidence_requests SET status='FULFILLED' "
                "WHERE subject_type=? AND subject_id=? AND status='OPEN'",
                (evidence["subject_type"], evidence["subject_id"]),
            )
            self._audit(actor, "EVIDENCE_APPROVE", "evidence", evidence_id,
                        {"subject_type": evidence["subject_type"], "subject_id": evidence["subject_id"]})
        return {"evidence_id": evidence_id, "status": "APPROVED"}

    def reject_evidence(self, actor: str, evidence_id: str, note: str) -> dict:
        evidence = self._evidence_row(evidence_id)
        self._check_decider(actor, evidence["submitted_by"])
        if evidence["status"] != "PENDING":
            raise ConflictError(f"证据当前状态为 {evidence['status']}，不能重复审批")
        if not note or not note.strip():
            raise ValidationError("驳回必须说明理由")
        with self.store.tx():
            if evidence["subject_type"] == EVIDENCE_SE_CLASSIFICATION:
                self.store.run(
                    "UPDATE projects SET pending_category_id=NULL WHERE project_id=?",
                    (evidence["subject_id"],),
                )
            elif evidence["subject_type"] == EVIDENCE_BASIC_RESEARCH:
                self.store.run(
                    "UPDATE rd_expenses SET pending_basic_research=0 WHERE expense_id=?",
                    (evidence["subject_id"],),
                )
            self.store.run(
                "UPDATE evidence SET status='REJECTED', decided_by=?, decided_at=?, decision_note=? "
                "WHERE evidence_id=?",
                (actor, self._now(), note, evidence_id),
            )
            self._audit(actor, "EVIDENCE_REJECT", "evidence", evidence_id, {"note": note})
        return {"evidence_id": evidence_id, "status": "REJECTED"}

    # ------------------------------------------------------------------
    # 更正版本：记录不可改，历史错误通过新版本处理
    # ------------------------------------------------------------------

    def _correction_target(self, entity_type: str) -> dict:
        target = CORRECTION_TARGETS.get(entity_type)
        if not target:
            raise ValidationError(f"不支持更正的实体类型: {entity_type}，允许值: {sorted(CORRECTION_TARGETS)}")
        return target

    def _normalize_changes(self, target: dict, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("更正内容不能为空")
        unknown = set(changes) - set(target["fields"])
        if unknown:
            raise ValidationError(f"不允许更正的字段: {sorted(unknown)}")
        normalized = {}
        for key, value in changes.items():
            column = target["fields"][key]
            if key in target["money"]:
                value = to_minor(value)
                if key == "amount" and value <= 0 and not changes.get("void"):
                    raise ValidationError("金额必须为正；如需作废请使用 void")
            elif key == "is_basic_research":
                value = 1 if value else 0
            elif key == "void":
                if value is not True:
                    raise ValidationError("void 仅接受 true（作废）")
                value = 1
            elif not isinstance(value, str) or not value.strip():
                raise ValidationError(f"字段 {key} 的内容无效")
            normalized[column] = value
        return normalized

    def create_correction(self, actor: str, entity_type: str, entity_id: str, changes: dict, reason: str) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        target = self._correction_target(entity_type)
        entity = self.store.q1(
            f"SELECT * FROM {target['table']} WHERE {target['pk']}=?", (entity_id,)
        )
        if not entity or entity["superseded_by"]:
            raise NotFoundError(f"记录不存在或已被更正: {entity_id}")
        if entity_type == "internal_transaction" and entity["status"] == "ELIMINATED":
            raise ConflictError("已抵销的内部交易不允许更正，请在开放期间记录反向交易")
        if not reason.strip():
            raise ValidationError("更正原因不能为空")
        normalized = self._normalize_changes(target, changes)
        correction_id = new_id("COR")
        with self.store.tx():
            self.store.run(
                "INSERT INTO corrections(correction_id, entity_type, entity_id, period, changes, reason, "
                "created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (correction_id, entity_type, entity_id, entity["period"], canonical(normalized),
                 reason, actor, self._now()),
            )
            self._audit(actor, "CORRECTION_CREATE", "correction", correction_id,
                        {"entity_type": entity_type, "entity_id": entity_id})
        return {"correction_id": correction_id}

    def _apply_correction_version(self, target: dict, entity, changes: dict, actor: str) -> str:
        """在实体最新版本上应用更正，生成新版本并接管有效状态。"""
        table = target["table"]
        pk = target["pk"]
        new_entity_id = new_id(target["prefix"])
        columns = [c for c in entity.keys() if c not in (pk, "version", "supersedes_id", "superseded_by",
                                                          "created_by", "created_at")]
        values = {c: entity[c] for c in columns}
        values.update(changes)
        if "pending_basic_research" in values and changes.get("is_basic_research"):
            values["pending_basic_research"] = 0
        placeholders = ", ".join(["?"] * (len(columns) + 6))
        self.store.run(
            f"INSERT INTO {table}({pk}, {', '.join(columns)}, version, supersedes_id, superseded_by, "
            f"created_by, created_at) VALUES({placeholders})",
            (new_entity_id, *[values[c] for c in columns], entity["version"] + 1, entity[pk], None,
             actor, self._now()),
        )
        self.store.run(
            f"UPDATE {table} SET superseded_by=? WHERE {pk}=?", (new_entity_id, entity[pk])
        )
        return new_entity_id

    def approve_correction(self, actor: str, correction_id: str) -> dict:
        correction = self.store.q1("SELECT * FROM corrections WHERE correction_id=?", (correction_id,))
        if not correction:
            raise NotFoundError(f"更正不存在: {correction_id}")
        self._check_decider(actor, correction["created_by"])
        if correction["status"] != "PENDING":
            raise ConflictError(f"更正当前状态为 {correction['status']}，不能重复审批")
        target = self._correction_target(correction["entity_type"])
        entity = self.store.q1(
            f"SELECT * FROM {target['table']} WHERE {target['pk']}=?", (correction["entity_id"],)
        )
        if not entity or entity["superseded_by"]:
            raise ConflictError("原记录已被其他更正取代，请基于最新版本重新发起更正")
        changes = json.loads(correction["changes"])
        with self.store.tx():
            new_entity_id = self._apply_correction_version(target, entity, changes, correction["created_by"])
            self.store.run(
                "UPDATE corrections SET status='APPROVED', new_entity_id=?, decided_by=?, decided_at=? "
                "WHERE correction_id=?",
                (new_entity_id, actor, self._now(), correction_id),
            )
            self._audit(actor, "CORRECTION_APPROVE", "correction", correction_id,
                        {"new_entity_id": new_entity_id})
        return {"correction_id": correction_id, "status": "APPROVED", "new_entity_id": new_entity_id}

    def reject_correction(self, actor: str, correction_id: str, note: str) -> dict:
        correction = self.store.q1("SELECT * FROM corrections WHERE correction_id=?", (correction_id,))
        if not correction:
            raise NotFoundError(f"更正不存在: {correction_id}")
        self._check_decider(actor, correction["created_by"])
        if correction["status"] != "PENDING":
            raise ConflictError(f"更正当前状态为 {correction['status']}，不能重复审批")
        if not note or not note.strip():
            raise ValidationError("驳回必须说明理由")
        with self.store.tx():
            self.store.run(
                "UPDATE corrections SET status='REJECTED', decided_by=?, decided_at=? WHERE correction_id=?",
                (actor, self._now(), correction_id),
            )
            self._audit(actor, "CORRECTION_REJECT", "correction", correction_id, {"note": note})
        return {"correction_id": correction_id, "status": "REJECTED"}

    def list_corrections(self, status: str | None = None) -> list:
        if status:
            rows = self.store.qa("SELECT * FROM corrections WHERE status=? ORDER BY created_at", (status,))
        else:
            rows = self.store.qa("SELECT * FROM corrections ORDER BY created_at")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 报告期关账：公式与组织范围随快照冻结（由可恢复作业执行）
    # ------------------------------------------------------------------

    def request_period_close(self, actor: str, period: str) -> dict:
        """提交关账作业并立即尝试执行；进程崩溃时由重启恢复继续。"""
        self._require(actor, *PLAN_ROLES)
        validate_period(period)
        job_id = self.enqueue_job(JOB_PERIOD_CLOSE, {"period": period, "actor": actor})
        self._run_job(job_id)
        return self.get_job(job_id)

    def _job_close_period(self, payload: dict) -> dict:
        snapshot_id = self._close_period(payload["actor"], payload["period"])
        return {"snapshot_id": snapshot_id}

    def _close_period(self, actor: str, period: str) -> str:
        with self.store.tx():
            row = self._period_row(period)
            if not row:
                raise NotFoundError(f"报告期不存在: {period}")
            if row["status"] == "CLOSED":
                return row["snapshot_id"]  # 幂等：重复关账返回既有快照
            if row["status"] != "OPEN":
                raise ConflictError(f"报告期 {period} 状态为 {row['status']}，不能关账")
            pending_evidence = self.store.q1(
                "SELECT COUNT(*) AS n FROM evidence WHERE status='PENDING' AND (period=? OR period IS NULL)",
                (period,),
            )["n"]
            if pending_evidence:
                raise ConflictError(f"存在 {pending_evidence} 项待审批证据，请先完成审批或驳回")
            pending_corrections = self.store.q1(
                "SELECT COUNT(*) AS n FROM corrections WHERE status='PENDING' AND period=?", (period,),
            )["n"]
            if pending_corrections:
                raise ConflictError(f"存在 {pending_corrections} 项待审批更正，请先完成审批或驳回")
            open_txns = self.store.q1(
                "SELECT COUNT(*) AS n FROM internal_transactions WHERE period=? AND status='OPEN' "
                "AND superseded_by IS NULL AND void=0",
                (period,),
            )["n"]
            if open_txns:
                raise ConflictError(f"存在 {open_txns} 笔未抵销的内部交易，请先执行抵销")
            version, definition = self._latest_formula()
            metrics, figures = self._metrics_for(period, definition)
            snapshot_id = new_id("SNP")
            closed_seq = self.store.q1("SELECT COALESCE(MAX(entry_id), 0) AS seq FROM audit_log")["seq"]
            self.store.run(
                "INSERT INTO snapshots(snapshot_id, period, formula_version, org_scope, targets, results, "
                "closed_seq, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    period,
                    version,
                    canonical(sorted(self._org_scope(period))),
                    canonical(self._active_targets(period)),
                    canonical({"metrics": metrics, "figures": figures}),
                    closed_seq,
                    actor,
                    self._now(),
                ),
            )
            self.store.run(
                "UPDATE periods SET status='CLOSED', closed_at=?, snapshot_id=? WHERE period=?",
                (self._now(), snapshot_id, period),
            )
            self._audit(actor, "PERIOD_CLOSE", "period", period,
                        {"snapshot_id": snapshot_id, "formula_version": version})
        return snapshot_id

    # ------------------------------------------------------------------
    # 批量导入：识别重送，冲突材料进入复核队列
    # ------------------------------------------------------------------

    def _apply_import(self, actor: str, item_type: str, payload: dict) -> tuple:
        if item_type == "EXPENSE":
            result = self.record_expense(
                actor, payload["project_code"], payload["period"], payload["amount"], payload["kind"]
            )
            if payload.get("is_basic_research"):
                # 导入声明的基础研究属性同样必须走证据审批，不得直接生效。
                self.claim_basic_research(actor, result["expense_id"], payload.get("basis", "批量导入声明"))
            return "rd_expense", result["expense_id"]
        if item_type == "TRANSFORMATION":
            result = self.record_transformation(
                actor, payload["project_code"], payload["period"], payload.get("revenue", 0),
                payload.get("value_added", 0), payload.get("description", ""),
            )
            return "transformation", result["transformation_id"]
        if item_type == "PUBLIC_SERVICE":
            result = self.record_public_service(
                actor, payload["unit_code"], payload["period"], payload["service_type"], payload["value"],
                payload["beneficiary_scope"], payload.get("description", ""),
            )
            return "public_service", result["contribution_id"]
        if item_type == "INVESTMENT":
            result = self.record_investment(
                actor, payload["project_code"], payload["period"], payload["amount"],
                payload["funding_source"], payload.get("note", ""),
            )
            return "investment_batch", result["batch_id"]
        if item_type == "INTERNAL_TXN":
            result = self.record_internal_transaction(
                actor, payload["seller_unit_code"], payload["buyer_unit_code"], payload["period"],
                payload["amount"], payload.get("value_added", 0), payload.get("project_code"),
                payload.get("description", ""),
            )
            return "internal_transaction", result["txn_id"]
        raise ValidationError(f"未知导入类型: {item_type}")

    def _open_review_for(self, item_id: str, idem_key: str, reason: str) -> str | None:
        existing = self.store.q1(
            "SELECT r.review_id FROM review_tasks r JOIN import_items i ON i.item_id = r.item_id "
            "WHERE i.idem_key=? AND r.status='OPEN'",
            (idem_key,),
        )
        if existing:
            return None
        review_id = new_id("REV")
        self.store.run(
            "INSERT INTO review_tasks(review_id, item_id, reason, created_at) VALUES(?, ?, ?, ?)",
            (review_id, item_id, reason, self._now()),
        )
        return review_id

    def import_batch(self, actor: str, source: str, items: list) -> dict:
        self._require(actor, *SUBMIT_ROLES)
        if not source or not source.strip():
            raise ValidationError("导入来源不能为空")
        if not isinstance(items, list) or not items:
            raise ValidationError("导入条目不能为空")
        batch_id = new_id("IMP")
        with self.store.tx():
            self.store.run(
                "INSERT INTO import_batches(batch_id, source, received_by, received_at) VALUES(?, ?, ?, ?)",
                (batch_id, source, actor, self._now()),
            )
        results = []
        counts = {"APPLIED": 0, "DUPLICATE": 0, "CONFLICT": 0}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"第 {index} 条导入条目必须是对象")
            key = item.get("key")
            item_type = item.get("type")
            payload = item.get("payload")
            if not key or item_type not in IMPORT_TYPES or not isinstance(payload, dict):
                raise ValidationError(f"第 {index} 条导入条目缺少 key/type/payload 或类型无效")
            digest = payload_hash(payload)
            with self.store.tx():
                latest = self.store.q1(
                    "SELECT * FROM import_items WHERE idem_key=? ORDER BY created_at DESC, item_id DESC LIMIT 1",
                    (key,),
                )
                item_id = new_id("IMI")
                status = None
                applied_entity = None
                review_id = None
                if latest and latest["payload_hash"] == digest:
                    status = "DUPLICATE"
                else:
                    applied_before = self.store.q1(
                        "SELECT * FROM import_items WHERE idem_key=? AND applied_entity IS NOT NULL "
                        "AND status IN ('APPLIED', 'RESOLVED') ORDER BY created_at DESC LIMIT 1",
                        (key,),
                    )
                    if applied_before:
                        status = "CONFLICT"
                        review_id = self._open_review_for(item_id, key, "与已接收材料内容不一致")
                    else:
                        try:
                            self.store.run("SAVEPOINT import_item_apply")
                            entity_type, entity_id = self._apply_import(actor, item_type, payload)
                            self.store.run("RELEASE import_item_apply")
                            status = "APPLIED"
                            applied_entity = f"{entity_type}:{entity_id}"
                        except DomainError as exc:
                            self.store.run("ROLLBACK TO import_item_apply")
                            self.store.run("RELEASE import_item_apply")
                            status = "CONFLICT"
                            review_id = self._open_review_for(item_id, key, f"材料无法入账：{exc.message}")
                self.store.run(
                    "INSERT INTO import_items(item_id, batch_id, idem_key, item_type, payload_hash, payload, "
                    "status, applied_entity, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item_id, batch_id, key, item_type, digest, canonical(payload), status,
                     applied_entity, self._now()),
                )
                counts[status] += 1
                results.append({"item_id": item_id, "key": key, "status": status,
                                "applied_entity": applied_entity, "review_id": review_id})
                self._audit(actor, "IMPORT_ITEM", "import_item", item_id,
                            {"key": key, "status": status, "batch_id": batch_id})
        with self.store.tx():
            self.store.run("UPDATE import_batches SET status='PROCESSED' WHERE batch_id=?", (batch_id,))
        return {"batch_id": batch_id, "counts": counts, "items": results}

    def get_import_batch(self, batch_id: str) -> dict:
        batch = self.store.q1("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise NotFoundError(f"导入批次不存在: {batch_id}")
        items = self.store.qa("SELECT * FROM import_items WHERE batch_id=? ORDER BY created_at", (batch_id,))
        return {"batch": dict(batch), "items": [dict(i) for i in items]}

    def list_reviews(self, status: str | None = None) -> list:
        if status:
            rows = self.store.qa("SELECT * FROM review_tasks WHERE status=? ORDER BY created_at", (status,))
        else:
            rows = self.store.qa("SELECT * FROM review_tasks ORDER BY created_at")
        return [dict(r) for r in rows]

    _IMPORT_CHANGE_MAP = {
        "EXPENSE": {"amount": "amount", "kind": "expense_kind"},
        "TRANSFORMATION": {"revenue": "revenue", "value_added": "value_added", "description": "description"},
        "PUBLIC_SERVICE": {"value": "value", "service_type": "service_type",
                           "beneficiary_scope": "beneficiary_scope", "description": "description"},
        "INVESTMENT": {"amount": "amount", "funding_source": "funding_source", "note": "note"},
        "INTERNAL_TXN": {"amount": "amount", "value_added": "value_added", "description": "description"},
    }

    def resolve_review(self, actor: str, review_id: str, action: str, note: str | None = None) -> dict:
        """复核处置：KEEP_EXISTING 保留原样；REPLACE 以新材料生成更正版本。"""
        self._require(actor, *REVIEW_ROLES)
        review = self.store.q1("SELECT * FROM review_tasks WHERE review_id=?", (review_id,))
        if not review:
            raise NotFoundError(f"复核任务不存在: {review_id}")
        if review["status"] != "OPEN":
            raise ConflictError("复核任务已处置")
        if action not in ("KEEP_EXISTING", "REPLACE"):
            raise ValidationError("处置方式必须是 KEEP_EXISTING 或 REPLACE")
        item = self.store.q1("SELECT * FROM import_items WHERE item_id=?", (review["item_id"],))
        payload = json.loads(item["payload"])
        with self.store.tx():
            applied_entity = item["applied_entity"]
            if action == "REPLACE":
                previous = self.store.q1(
                    "SELECT * FROM import_items WHERE idem_key=? AND applied_entity IS NOT NULL "
                    "AND status IN ('APPLIED', 'RESOLVED') AND item_id<>? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (item["idem_key"], item["item_id"]),
                )
                if previous:
                    entity_type, entity_id = previous["applied_entity"].split(":", 1)
                    target = self._correction_target(entity_type)
                    entity = self.store.q1(
                        f"SELECT * FROM {target['table']} WHERE {target['pk']}=?", (entity_id,)
                    )
                    if not entity or entity["superseded_by"]:
                        raise ConflictError("原记录已被其他更正取代，请人工处理")
                    field_map = self._IMPORT_CHANGE_MAP[item["item_type"]]
                    changes = {out: payload[src] for src, out in field_map.items() if src in payload}
                    normalized = self._normalize_changes(target, changes)
                    new_entity_id = self._apply_correction_version(target, entity, normalized, actor)
                    correction_id = new_id("COR")
                    self.store.run(
                        "INSERT INTO corrections(correction_id, entity_type, entity_id, period, changes, reason, "
                        "status, new_entity_id, created_by, created_at, decided_by, decided_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, ?)",
                        (correction_id, entity_type, entity_id, entity["period"], canonical(normalized),
                         f"导入复核替换（复核 {review_id}）", new_entity_id, actor, self._now(),
                         actor, self._now()),
                    )
                    self._audit(actor, "CORRECTION_APPROVE", "correction", correction_id,
                                {"via": f"review {review_id}"})
                    applied_entity = f"{entity_type}:{new_entity_id}"
                else:
                    entity_type, entity_id = self._apply_import(actor, item["item_type"], payload)
                    applied_entity = f"{entity_type}:{entity_id}"
            self.store.run(
                "UPDATE import_items SET status='RESOLVED', applied_entity=? WHERE item_id=?",
                (applied_entity, item["item_id"]),
            )
            self.store.run(
                "UPDATE review_tasks SET status='RESOLVED', resolution=?, resolved_by=?, resolved_at=? "
                "WHERE review_id=?",
                (action if not note else f"{action}: {note}", actor, self._now(), review_id),
            )
            self._audit(actor, "REVIEW_RESOLVE", "review_task", review_id,
                        {"action": action, "item_id": item["item_id"]})
        return {"review_id": review_id, "status": "RESOLVED", "applied_entity": applied_entity}

    # ------------------------------------------------------------------
    # 证据催补与可恢复作业
    # ------------------------------------------------------------------

    def create_evidence_request(
        self, actor: str, subject_type: str, subject_id: str, unit_code: str, description: str, due_at: str
    ) -> dict:
        self._require(actor, *PLAN_ROLES, ROLE_REVIEWER)
        unit = self._unit_by_code(unit_code)
        if not description.strip():
            raise ValidationError("催补说明不能为空")
        due = parse_instant(due_at)
        request_id = new_id("EVR")
        with self.store.tx():
            self.store.run(
                "INSERT INTO evidence_requests(request_id, subject_type, subject_id, unit_id, description, "
                "due_at, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, subject_type, subject_id, unit["unit_id"], description,
                 due.isoformat(), actor, self._now()),
            )
            self._audit(actor, "EVIDENCE_REQUEST", "evidence_request", request_id,
                        {"subject_type": subject_type, "subject_id": subject_id})
        return {"request_id": request_id}

    def run_reminder_sweep(self) -> dict:
        """扫描超期未审批证据与逾期未补证据请求，生成催补提醒（幂等）。"""
        now = self.clock().astimezone(timezone.utc)
        cutoff = now - self.evidence_sla
        created = 0
        with self.store.tx():
            for row in self.store.qa("SELECT evidence_id, submitted_at FROM evidence WHERE status='PENDING'"):
                if parse_instant(row["submitted_at"]) < cutoff:
                    cursor = self.store.run(
                        "INSERT OR IGNORE INTO reminders(kind, ref_id, message, created_at) VALUES(?, ?, ?, ?)",
                        ("EVIDENCE_OVERDUE", row["evidence_id"],
                         f"证据 {row['evidence_id']} 待审批已超期", self._now()),
                    )
                    created += cursor.rowcount
            for row in self.store.qa(
                "SELECT request_id, due_at FROM evidence_requests WHERE status='OPEN'"
            ):
                if parse_instant(row["due_at"]) < now:
                    cursor = self.store.run(
                        "INSERT OR IGNORE INTO reminders(kind, ref_id, message, created_at) VALUES(?, ?, ?, ?)",
                        ("REQUEST_OVERDUE", row["request_id"],
                         f"证据催补请求 {row['request_id']} 已逾期", self._now()),
                    )
                    created += cursor.rowcount
        return {"created": created}

    def list_reminders(self) -> list:
        return [dict(r) for r in self.store.qa("SELECT * FROM reminders ORDER BY reminder_id")]

    def enqueue_job(self, job_type: str, payload: dict) -> str:
        if job_type not in self._job_handlers:
            raise ValidationError(f"未知作业类型: {job_type}")
        job_id = new_id("JOB")
        with self.store.tx():
            self.store.run(
                "INSERT INTO jobs(job_id, job_type, payload, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (job_id, job_type, canonical(payload), self._now(), self._now()),
            )
        return job_id

    def get_job(self, job_id: str) -> dict:
        row = self.store.q1("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not row:
            raise NotFoundError(f"作业不存在: {job_id}")
        result = dict(row)
        if result.get("result"):
            result["result"] = json.loads(result["result"])
        return result

    def list_jobs(self) -> list:
        return [dict(r) for r in self.store.qa("SELECT * FROM jobs ORDER BY created_at")]

    def _run_job(self, job_id: str) -> None:
        with self.store.tx():
            job = self.store.q1("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            if not job or job["status"] != "PENDING":
                return
            self.store.run(
                "UPDATE jobs SET status='RUNNING', attempts=attempts+1, updated_at=? WHERE job_id=?",
                (self._now(), job_id),
            )
            job_type, payload = job["job_type"], json.loads(job["payload"])
        try:
            result = self._job_handlers[job_type](payload)
        except Exception as exc:  # 作业失败留痕，等待人工或重启恢复
            with self.store.tx():
                self.store.run(
                    "UPDATE jobs SET status='FAILED', error=?, updated_at=? WHERE job_id=?",
                    (str(exc), self._now(), job_id),
                )
            return
        with self.store.tx():
            self.store.run(
                "UPDATE jobs SET status='DONE', result=?, updated_at=? WHERE job_id=?",
                (canonical(result), self._now(), job_id),
            )

    def run_pending_jobs(self) -> None:
        for row in self.store.qa("SELECT job_id FROM jobs WHERE status='PENDING' ORDER BY created_at"):
            self._run_job(row["job_id"])

    def recover(self) -> None:
        """进程重启后恢复：中断的作业重新排队，催补扫描不丢。"""
        with self.store.tx():
            self.store.run(
                "UPDATE jobs SET status='PENDING', updated_at=? WHERE status='RUNNING'", (self._now(),)
            )
        if not self.store.q1(
            "SELECT 1 FROM jobs WHERE job_type=? AND status IN ('PENDING', 'RUNNING')",
            (JOB_REMINDER_SWEEP,),
        ):
            self.enqueue_job(JOB_REMINDER_SWEEP, {})
        self.run_pending_jobs()

    def _job_reminder_sweep(self, payload: dict) -> dict:
        return self.run_reminder_sweep()

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def audit_trail(self, entity_type: str | None = None, entity_id: str | None = None) -> list:
        sql = "SELECT * FROM audit_log"
        args = []
        if entity_type:
            sql += " WHERE entity_type=?"
            args.append(entity_type)
            if entity_id:
                sql += " AND entity_id=?"
                args.append(entity_id)
        sql += " ORDER BY entry_id"
        return [dict(r) for r in self.store.qa(sql, tuple(args))]
