"""价值贡献治理应用服务。

一个 ``GovernanceService`` 实例绑定一个 SQLite 连接，承载全部用例：
组织/产业主数据、项目与分摊规则、贡献记录、特殊归类审批、批量导入
去重与冲突复核、报告期目标、关账快照、更正版本。

权限模型：

- DATA_OFFICE  集团数据办公室：维护主数据、发起关账与更正
- PLANNER      规划管理人员：只在开放期维护规划目标
- REVIEWER     财务复核人员：批准特殊归类、裁决冲突、复核更正
- UNIT_USER    业务单位：只能向本单位报送记录与提交证据，无权批准
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from . import engine
from .db import open_store
from .errors import (
    InvalidStateError,
    NotFoundError,
    PeriodClosedError,
    PermissionDeniedError,
    ValidationError,
)

ROLE_DATA_OFFICE = "DATA_OFFICE"
ROLE_PLANNER = "PLANNER"
ROLE_REVIEWER = "REVIEWER"
ROLE_UNIT = "UNIT_USER"

ONE = Decimal("1")
SHARE_Q = Decimal("0.0001")

ROW_KINDS = {
    "OUTPUT",
    "RD_EXPENSE",
    "TRANSFORMATION",
    "PUBLIC_SERVICE",
    "INTERNAL_TRADE",
}
CORRECTABLE_SECTIONS = {
    "outputs",
    "rd_expenses",
    "transformations",
    "public_services",
    "internal_trades",
}
# 更正版本允许触碰的业务列；报送单位、报告期、审批留痕一律不可改
CORRECTION_ALLOWED_COLUMNS = {
    "outputs": {"revenue", "value_added", "claimed_strategic",
                "evidence_ref"},
    "rd_expenses": {"amount", "basic_claim", "evidence_ref"},
    "transformations": {"revenue", "value_added", "evidence_ref"},
    "public_services": {"amount", "metric_text", "evidence_ref",
                        "project_code"},
    "internal_trades": {"revenue_amount", "value_added_amount"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any, field: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field}金额无效：{value!r}")
    if parsed < 0:
        raise ValidationError(f"{field}金额不能为负")
    return str(parsed)


def _shares_total(shares: dict[str, str]) -> Decimal:
    total = Decimal("0")
    for org, value in shares.items():
        try:
            share = Decimal(str(value)).quantize(SHARE_Q)
        except (InvalidOperation, ValueError):
            raise ValidationError(f"单位{org}的份额无效：{value!r}")
        if share <= 0:
            raise ValidationError(f"单位{org}的份额必须为正")
        total += share
    return total


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GovernanceService:
    def __init__(self, conn: sqlite3.Connection | str = ":memory:"):
        self.conn = open_store(conn) if isinstance(conn, str) else conn

    # -- 内部工具 ----------------------------------------------------------
    def _audit(self, actor: str, action: str, entity: str, entity_id: Any,
               detail: Any = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, actor, action, entity, entity_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (_now(), actor, action, entity, str(entity_id),
             detail if isinstance(detail, str) else _canonical(detail)),
        )

    def _user(self, username: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"用户不存在：{username}")
        return row

    def _require_role(self, username: str, roles: Iterable[str]) -> sqlite3.Row:
        user = self._user(username)
        if user["role"] not in set(roles):
            raise PermissionDeniedError(
                f"角色{user['role']}无权执行该操作（需要 {'/'.join(roles)}）"
            )
        return user

    def _period(self, period_code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM periods WHERE code=?", (period_code,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"报告期不存在：{period_code}")
        return row

    def _require_open(self, period_code: str) -> sqlite3.Row:
        period = self._period(period_code)
        if period["status"] != "OPEN":
            raise PeriodClosedError(
                f"报告期{period_code}已关账，公式、组织范围与历史数据已冻结"
            )
        return period

    def _assert_org_exists(self, org_code: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM organizations WHERE code=?", (org_code,)
        ).fetchone() is None:
            raise NotFoundError(f"组织不存在：{org_code}")

    def _assert_unit_scope(self, user: sqlite3.Row, org_code: str) -> None:
        if user["role"] == ROLE_UNIT and user["org_code"] != org_code:
            raise PermissionDeniedError(
                f"业务单位只能维护本单位（{user['org_code']}）数据，"
                f"不得替 {org_code} 报送"
            )

    def _assert_unit_on_project(self, user: sqlite3.Row, period_code: str,
                                project_code: str) -> None:
        """业务单位只能报送本单位牵头或已列入分摊规则的项目。"""
        if user["role"] != ROLE_UNIT:
            return
        project = self.conn.execute(
            "SELECT lead_org FROM projects WHERE code=?", (project_code,)
        ).fetchone()
        if project is None:
            raise NotFoundError(f"项目不存在：{project_code}")
        if project["lead_org"] == user["org_code"]:
            return
        participant = self.conn.execute(
            "SELECT 1 FROM allocation_shares ash"
            " JOIN allocation_rule_sets rs ON ash.rule_set_id=rs.id"
            " WHERE rs.period_code=? AND rs.project_code=? AND rs.active=1"
            " AND ash.org_code=?",
            (period_code, project_code, user["org_code"]),
        ).fetchone()
        if participant is None:
            raise PermissionDeniedError(
                f"单位{user['org_code']}不是项目{project_code}的牵头方或"
                "经批准的协作方，不能就该项目报送"
            )

    # -- 身份与主数据 ------------------------------------------------------
    def create_user(self, username: str, display_name: str, role: str,
                    org_code: str | None = None) -> None:
        if role == ROLE_UNIT and not org_code:
            raise ValidationError("业务单位用户必须归属一个组织")
        if org_code:
            self._assert_org_exists(org_code)
        self.conn.execute(
            "INSERT INTO users(username, display_name, role, org_code, created_at)"
            " VALUES (?,?,?,?,?)",
            (username, display_name, role, org_code, _now()),
        )

    def create_organization(self, username: str, code: str, name: str,
                            org_type: str, parent_code: str | None = None) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        if parent_code:
            self._assert_org_exists(parent_code)
        self.conn.execute(
            "INSERT INTO organizations(code, name, org_type, parent_code)"
            " VALUES (?,?,?,?)",
            (code, name, org_type, parent_code),
        )
        self._audit(username, "CREATE", "organization", code, name)

    def create_industry(self, username: str, code: str, name: str,
                        is_strategic: bool = False) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        self.conn.execute(
            "INSERT INTO industry_catalog(code, name, is_strategic)"
            " VALUES (?,?,?)",
            (code, name, 1 if is_strategic else 0),
        )
        self._audit(username, "CREATE", "industry", code,
                    {"strategic": is_strategic})

    def register_reorg(self, username: str, period_code: str, kind: str,
                       description: str,
                       mappings: list[tuple[str, str, str]]) -> int:
        """登记重组事件。mappings 为 (源单位, 目标单位, 权重)；每个源单位
        转出权重合计必须恰为 1。"""
        self._require_role(username, {ROLE_DATA_OFFICE})
        self._require_open(period_code)
        for source, target, _ in mappings:
            self._assert_org_exists(source)
            self._assert_org_exists(target)
        by_source: dict[str, Decimal] = {}
        for source, _, weight in mappings:
            by_source[source] = by_source.get(source, Decimal("0")) + Decimal(weight)
        for source, total in by_source.items():
            if total.quantize(SHARE_Q) != ONE:
                raise ValidationError(
                    f"重组源单位{source}的转出权重合计为{total}，必须等于1"
                )
        cur = self.conn.execute(
            "INSERT INTO reorg_events(effective_period, kind, description,"
            " created_by, created_at) VALUES (?,?,?,?,?)",
            (period_code, kind, description, username, _now()),
        )
        event_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO reorg_mappings(event_id, source_org, target_org, weight)"
            " VALUES (?,?,?,?)",
            [(event_id, s, t, w) for s, t, w in mappings],
        )
        self._audit(username, "REORG", "reorg_event", event_id,
                    {"mappings": mappings})
        return event_id

    # -- 项目、任务、批次与拆分 --------------------------------------------
    def create_project(self, username: str, code: str, name: str,
                       lead_org: str, industry_code: str,
                       parent_project: str | None = None) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        self._assert_org_exists(lead_org)
        if self.conn.execute(
            "SELECT 1 FROM industry_catalog WHERE code=?", (industry_code,)
        ).fetchone() is None:
            raise NotFoundError(f"产业分类不存在：{industry_code}")
        self.conn.execute(
            "INSERT INTO projects(code, name, lead_org, industry_code,"
            " parent_project, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (code, name, lead_org, industry_code, parent_project,
             username, _now()),
        )
        self._audit(username, "CREATE", "project", code, name)

    def create_task(self, username: str, code: str, project_code: str,
                    name: str) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        self.conn.execute(
            "INSERT INTO project_tasks(code, project_code, name) VALUES (?,?,?)",
            (code, project_code, name),
        )

    def create_investment_batch(self, username: str, code: str,
                                project_code: str, period_code: str,
                                name: str, amount: str,
                                task_code: str | None = None) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        self._require_open(period_code)
        self.conn.execute(
            "INSERT INTO investment_batches(code, project_code, task_code,"
            " period_code, name, amount) VALUES (?,?,?,?,?,?)",
            (code, project_code, task_code, period_code, name,
             _money(amount, "投资")),
        )

    def split_project(self, username: str, parent_project: str,
                      period_code: str,
                      children: list[tuple[str, str]]) -> None:
        """项目拆分：(子项目编码, 权重) 合计必须为 1。子项目须先立项。"""
        self._require_role(username, {ROLE_DATA_OFFICE})
        self._require_open(period_code)
        if not children:
            raise ValidationError("拆分至少要有一个子项目")
        total = Decimal("0")
        for child, weight in children:
            row = self.conn.execute(
                "SELECT parent_project, status FROM projects WHERE code=?",
                (child,),
            ).fetchone()
            if row is None or row["parent_project"] != parent_project:
                raise ValidationError(f"{child}不是{parent_project}的子项目")
            total += Decimal(weight)
        if total.quantize(SHARE_Q) != ONE:
            raise ValidationError(f"拆分权重合计为{total}，必须等于1")
        self.conn.execute(
            "UPDATE projects SET status='SPLIT' WHERE code=?", (parent_project,)
        )
        for child, weight in children:
            self.conn.execute(
                "INSERT INTO project_splits(parent_project, child_project,"
                " weight, effective_period, created_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (parent_project, child, weight, period_code, username, _now()),
            )
        self._audit(username, "SPLIT", "project", parent_project,
                    {"children": children})

    def define_allocation(self, username: str, period_code: str,
                          project_code: str, basis: str,
                          shares: dict[str, str],
                          task_code: str | None = None,
                          batch_code: str | None = None,
                          note: str = "") -> int:
        """登记协作分摊规则（新版本，旧版本自动失效）。份额合计必须为 1。"""
        self._require_role(username, {ROLE_DATA_OFFICE})
        self._require_open(period_code)
        if len(shares) < 2:
            raise ValidationError("联合贡献分摊至少涉及两个单位")
        total = _shares_total(shares)
        if total != ONE:
            raise ValidationError(f"分摊份额合计为{total}，必须等于1")
        for org in shares:
            self._assert_org_exists(org)
        scope = self.conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM allocation_rule_sets"
            " WHERE period_code=? AND project_code=?"
            " AND COALESCE(task_code,'')=COALESCE(?,'')"
            " AND COALESCE(batch_code,'')=COALESCE(?,'')",
            (period_code, project_code, task_code, batch_code),
        ).fetchone()["v"]
        next_version = scope + 1
        cur = self.conn.execute(
            "INSERT INTO allocation_rule_sets(project_code, task_code,"
            " batch_code, period_code, basis, note, version, created_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project_code, task_code, batch_code, period_code, basis, note,
             next_version, username, _now()),
        )
        rule_set_id = cur.lastrowid
        # 同期间同作用域的旧版本失效（只新增不删除，版本链可审计）
        self.conn.execute(
            "UPDATE allocation_rule_sets SET active=0 WHERE id<>?"
            " AND period_code=? AND project_code=?"
            " AND COALESCE(task_code,'')=COALESCE(?,'')"
            " AND COALESCE(batch_code,'')=COALESCE(?,'')",
            (rule_set_id, period_code, project_code, task_code, batch_code),
        )
        self.conn.executemany(
            "INSERT INTO allocation_shares(rule_set_id, org_code, share)"
            " VALUES (?,?,?)",
            [(rule_set_id, org, str(Decimal(v).quantize(SHARE_Q)))
             for org, v in shares.items()],
        )
        self._audit(username, "ALLOCATE", "rule_set", rule_set_id,
                    {"project": project_code, "shares": shares,
                     "basis": basis, "version": next_version})
        return rule_set_id

    # -- 报告期与目标 ------------------------------------------------------
    def create_period(self, username: str, code: str, name: str,
                      prior_period_code: str | None = None) -> None:
        self._require_role(username, {ROLE_DATA_OFFICE})
        if prior_period_code:
            self._period(prior_period_code)
        self.conn.execute(
            "INSERT INTO periods(code, name, prior_period_code) VALUES (?,?,?)",
            (code, name, prior_period_code),
        )

    def set_goal(self, username: str, period_code: str, metric: str,
                 target_value: str, baseline_value: str | None = None,
                 note: str = "") -> None:
        """规划目标只能由规划人员修改，且只影响开放期间。"""
        self._require_role(username, {ROLE_PLANNER})
        self._require_open(period_code)
        self.conn.execute(
            "INSERT INTO period_goals(period_code, metric, target_value,"
            " baseline_value, note, updated_by, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(period_code, metric) DO UPDATE SET"
            " target_value=excluded.target_value,"
            " baseline_value=excluded.baseline_value, note=excluded.note,"
            " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (period_code, metric, _money(target_value, "目标"),
             baseline_value, note, username, _now()),
        )
        self._audit(username, "SET_GOAL", "goal", f"{period_code}:{metric}",
                    target_value)

    # -- 贡献记录 ----------------------------------------------------------
    def _check_project_writable(self, project_code: str) -> None:
        row = self.conn.execute(
            "SELECT status FROM projects WHERE code=?", (project_code,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"项目不存在：{project_code}")
        if row["status"] == "SPLIT":
            raise InvalidStateError(
                f"项目{project_code}已拆分，不能再向父项目报送，请改报子项目"
            )

    def report_output(self, username: str, period_code: str, project_code: str,
                      revenue: str, value_added: str,
                      claimed_strategic: bool = False,
                      evidence_ref: str = "",
                      task_code: str | None = None,
                      batch_code: str | None = None,
                      _allow_closed: bool = False) -> int:
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        if not _allow_closed:
            self._require_open(period_code)
        self._check_project_writable(project_code)
        self._assert_unit_scope(user, user["org_code"])
        self._assert_unit_on_project(user, period_code, project_code)
        cur = self.conn.execute(
            "INSERT INTO outputs(period_code, project_code, task_code,"
            " batch_code, revenue, value_added, claimed_strategic,"
            " evidence_ref, reported_by, reported_org, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (period_code, project_code, task_code, batch_code,
             _money(revenue, "营业收入"), _money(value_added, "增加值"),
             1 if claimed_strategic else 0, evidence_ref, username,
             user["org_code"], _now()),
        )
        return cur.lastrowid

    def report_rd_expense(self, username: str, period_code: str,
                          project_code: str, amount: str,
                          basic_claim: bool = False, evidence_ref: str = "",
                          task_code: str | None = None,
                          batch_code: str | None = None,
                          _allow_closed: bool = False) -> int:
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        if not _allow_closed:
            self._require_open(period_code)
        self._check_project_writable(project_code)
        self._assert_unit_scope(user, user["org_code"])
        self._assert_unit_on_project(user, period_code, project_code)
        cur = self.conn.execute(
            "INSERT INTO rd_expenses(period_code, project_code, task_code,"
            " batch_code, amount, basic_claim, evidence_ref, reported_by,"
            " reported_org, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (period_code, project_code, task_code, batch_code,
             _money(amount, "研发费用"), 1 if basic_claim else 0,
             evidence_ref, username, user["org_code"], _now()),
        )
        return cur.lastrowid

    def report_transformation(self, username: str, period_code: str,
                              project_code: str, revenue: str, value_added: str,
                              evidence_ref: str = "",
                              batch_code: str | None = None,
                              _allow_closed: bool = False) -> int:
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        if not _allow_closed:
            self._require_open(period_code)
        self._check_project_writable(project_code)
        self._assert_unit_scope(user, user["org_code"])
        self._assert_unit_on_project(user, period_code, project_code)
        cur = self.conn.execute(
            "INSERT INTO transformations(period_code, project_code,"
            " batch_code, revenue, value_added, evidence_ref, reported_by,"
            " reported_org, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (period_code, project_code, batch_code,
             _money(revenue, "转化收入"), _money(value_added, "转化增加值"),
             evidence_ref, username, user["org_code"], _now()),
        )
        return cur.lastrowid

    def report_public_service(self, username: str, period_code: str,
                              org_code: str, amount: str,
                              metric_text: str = "", evidence_ref: str = "",
                              project_code: str | None = None,
                              _allow_closed: bool = False) -> int:
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        if not _allow_closed:
            self._require_open(period_code)
        self._assert_org_exists(org_code)
        self._assert_unit_scope(user, org_code)
        cur = self.conn.execute(
            "INSERT INTO public_services(period_code, org_code, project_code,"
            " amount, metric_text, evidence_ref, reported_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (period_code, org_code, project_code, _money(amount, "公共服务"),
             metric_text, evidence_ref, username, _now()),
        )
        return cur.lastrowid

    # -- 内部交易 ----------------------------------------------------------
    def record_internal_trade(self, username: str, period_code: str,
                              seller_org: str, buyer_org: str,
                              revenue_amount: str, value_added_amount: str,
                              project_code: str | None = None,
                              _allow_closed: bool = False) -> int:
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        if not _allow_closed:
            self._require_open(period_code)
        self._assert_org_exists(seller_org)
        self._assert_org_exists(buyer_org)
        if seller_org == buyer_org:
            raise ValidationError("内部交易买卖双方必须是不同单位")
        self._assert_unit_scope(user, seller_org)
        cur = self.conn.execute(
            "INSERT INTO internal_trades(period_code, project_code,"
            " seller_org, buyer_org, revenue_amount, value_added_amount,"
            " status, recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (period_code, project_code, seller_org, buyer_org,
             _money(revenue_amount, "内部交易收入"),
             _money(value_added_amount, "内部交易增加值"),
             "PENDING", username, _now()),
        )
        return cur.lastrowid

    def confirm_internal_trade(self, username: str, trade_id: int) -> None:
        """买方单位双边确认后方可进入抵销。"""
        user = self._require_role(username, {ROLE_UNIT, ROLE_REVIEWER,
                                             ROLE_DATA_OFFICE})
        trade = self.conn.execute(
            "SELECT * FROM internal_trades WHERE id=?", (trade_id,)
        ).fetchone()
        if trade is None:
            raise NotFoundError(f"内部交易不存在：{trade_id}")
        if trade["status"] != "PENDING":
            raise InvalidStateError("仅待确认的交易可以确认")
        if user["role"] == ROLE_UNIT and user["org_code"] != trade["buyer_org"]:
            raise PermissionDeniedError("只有买方单位可以确认该笔内部交易")
        self.conn.execute(
            "UPDATE internal_trades SET status='MATCHED', confirmed_by=?,"
            " confirmed_at=? WHERE id=?",
            (username, _now(), trade_id),
        )
        self._audit(username, "CONFIRM_TRADE", "internal_trade", trade_id)

    def dispute_internal_trade(self, username: str, trade_id: int,
                               note: str) -> None:
        self._require_role(username, {ROLE_UNIT, ROLE_REVIEWER,
                                      ROLE_DATA_OFFICE})
        self.conn.execute(
            "UPDATE internal_trades SET status='DISPUTED', dispute_note=?"
            " WHERE id=?",
            (note, trade_id),
        )
        self.conn.execute(
            "INSERT INTO review_queue(ref_type, ref_id, reason, created_by,"
            " created_at) VALUES ('TRADE_DISPUTE',?,?,?,?)",
            (trade_id, note, username, _now()),
        )

    def resolve_trade_dispute(self, username: str, trade_id: int,
                              resolution: str, matched: bool) -> None:
        """争议交易由复核人员裁决，业务单位无权自行处理。"""
        self._require_role(username, {ROLE_REVIEWER})
        self.conn.execute(
            "UPDATE internal_trades SET status=?, confirmed_by=?, confirmed_at=?"
            " WHERE id=?",
            ("MATCHED" if matched else "PENDING", username, _now(), trade_id),
        )
        self.conn.execute(
            "UPDATE review_queue SET status='RESOLVED', resolved_by=?,"
            " resolved_at=?, resolution=? WHERE ref_type='TRADE_DISPUTE'"
            " AND ref_id=? AND status='OPEN'",
            (username, _now(), resolution, trade_id),
        )
        self._audit(username, "RESOLVE_TRADE", "internal_trade", trade_id,
                    resolution)

    # -- 特殊归类审批 ------------------------------------------------------
    def submit_classification(self, username: str, period_code: str,
                              kind: str, target_ref: str, claimed_value: str,
                              evidence_ref: str) -> int:
        """业务单位提交特殊归类证据；提交后只能等待复核，不能自行批准。"""
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        self._require_open(period_code)
        if not evidence_ref.strip():
            raise ValidationError("特殊归类必须附证据材料编号")
        self._validate_classification_target(kind, target_ref)
        cur = self.conn.execute(
            "INSERT INTO classification_requests(period_code, kind,"
            " target_ref, claimed_value, evidence_ref, status, submitted_by,"
            " submitted_org, submitted_at) VALUES (?,?,?,?,?,'SUBMITTED',?,?,?)",
            (period_code, kind, target_ref, str(claimed_value),
             evidence_ref, username, user["org_code"] or "", _now()),
        )
        return cur.lastrowid

    def _validate_classification_target(self, kind: str, target_ref: str) -> None:
        if kind == "PROJECT_INDUSTRY":
            if self.conn.execute("SELECT 1 FROM projects WHERE code=?",
                                 (target_ref,)).fetchone() is None:
                raise NotFoundError(f"项目不存在：{target_ref}")
        elif kind == "OUTPUT_STRATEGIC":
            if self.conn.execute("SELECT 1 FROM outputs WHERE id=?",
                                 (target_ref,)).fetchone() is None:
                raise NotFoundError(f"产出记录不存在：{target_ref}")
        elif kind == "RD_BASIC":
            if self.conn.execute("SELECT 1 FROM rd_expenses WHERE id=?",
                                 (target_ref,)).fetchone() is None:
                raise NotFoundError(f"研发费用记录不存在：{target_ref}")
        else:
            raise ValidationError(f"未知归类类型：{kind}")

    def review_classification(self, username: str, request_id: int,
                              approve: bool, note: str = "") -> None:
        """复核人员批准或驳回；任何人都不能批准自己提交的申请。"""
        reviewer = self._require_role(username, {ROLE_REVIEWER})
        req = self.conn.execute(
            "SELECT * FROM classification_requests WHERE id=?", (request_id,)
        ).fetchone()
        if req is None:
            raise NotFoundError(f"归类申请不存在：{request_id}")
        if req["status"] != "SUBMITTED":
            raise InvalidStateError("该申请已复核")
        if req["submitted_by"] == username:
            raise PermissionDeniedError("提交人不得批准自己的特殊归类申请")
        status = "APPROVED" if approve else "REJECTED"
        self.conn.execute(
            "UPDATE classification_requests SET status=?, reviewed_by=?,"
            " reviewed_at=?, review_note=? WHERE id=?",
            (status, username, _now(), note, request_id),
        )
        if approve and req["kind"] == "PROJECT_INDUSTRY":
            industry = req["claimed_value"]
            if self.conn.execute(
                "SELECT 1 FROM industry_catalog WHERE code=?", (industry,)
            ).fetchone() is None:
                raise NotFoundError(f"产业分类不存在：{industry}")
            self.conn.execute(
                "UPDATE projects SET industry_code=? WHERE code=?",
                (industry, req["target_ref"]),
            )
        self._audit(username, "REVIEW_CLASSIFICATION",
                    "classification_request", request_id,
                    {"approve": approve, "note": note})

    # -- 批量导入 ----------------------------------------------------------
    @staticmethod
    def _dedup_key(period_code: str, row: dict[str, Any]) -> str:
        identity = [
            period_code,
            row["row_kind"],
            str(row.get("line_ref", "")),
            str(row.get("reported_org") or row.get("seller_org") or ""),
            str(row.get("project_code") or ""),
        ]
        return "|".join(identity)

    def submit_import_batch(self, username: str, period_code: str,
                            client_ref: str,
                            rows: list[dict[str, Any]]) -> dict[str, Any]:
        """批量导入；按客户批次号识别重送，行级去重，冲突材料交复核。

        返回批次摘要：status 为 RESEND（完全重送，原样忽略）、DONE（全部
        落库）或 CONFLICT_REVIEW（存在冲突行，等待复核裁决）。
        """
        user = self._require_role(username, {ROLE_UNIT, ROLE_DATA_OFFICE})
        self._require_open(period_code)
        if not rows:
            raise ValidationError("导入内容为空")
        fingerprint = _sha256(_canonical(rows))
        existing = self.conn.execute(
            "SELECT * FROM import_batches WHERE period_code=? AND client_ref=?",
            (period_code, client_ref),
        ).fetchone()
        if existing is not None:
            if existing["fingerprint"] == fingerprint:
                self._audit(username, "IMPORT_RESEND", "import_batch",
                            existing["id"], "完全重送，已幂等忽略")
                return {"batch_id": existing["id"], "status": "RESEND",
                        "applied": 0, "conflicts": 0, "duplicates": len(rows)}
            raise InvalidStateError(
                f"批次号{client_ref}曾以不同内容报送，禁止覆盖；"
                "请使用新批次号，冲突行须经复核裁决"
            )

        cur = self.conn.execute(
            "INSERT INTO import_batches(period_code, client_ref,"
            " fingerprint, status, submitted_by, row_count, created_at)"
            " VALUES (?,?,?,'RECEIVED',?,?,?)",
            (period_code, client_ref, fingerprint, username, len(rows),
             _now()),
        )
        batch_id = cur.lastrowid
        applied = duplicates = conflicts = 0
        for line_no, row in enumerate(sorted(rows, key=_canonical), start=1):
            self._validate_import_row(user, period_code, row)
            dedup_key = self._dedup_key(period_code, row)
            payload_hash = _sha256(_canonical(row))
            # 期内历史行去重：相同业务凭证再次报送，内容一致即重送，
            # 内容不一致即为冲突材料，挂复核队列
            prior = self.conn.execute(
                "SELECT ir.* FROM import_rows ir JOIN import_batches ib"
                " ON ir.batch_id=ib.id WHERE ir.dedup_key=?"
                " AND ir.status IN ('NEW','APPLIED')"
                " ORDER BY ir.id DESC LIMIT 1",
                (dedup_key,),
            ).fetchone()
            if prior is not None and prior["payload_hash"] == payload_hash:
                status, reason = "DUPLICATE", "重送：与已接收内容一致"
                duplicates += 1
            elif prior is not None:
                status, reason = "CONFLICT", (
                    f"与既有材料（行{prior['id']}）内容不一致，待复核"
                )
                conflicts += 1
            else:
                status, reason = "NEW", ""
            cur2 = self.conn.execute(
                "INSERT INTO import_rows(batch_id, line_no, row_kind,"
                " dedup_key, payload_hash, payload, status, conflict_reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, line_no, row["row_kind"], dedup_key,
                 payload_hash, _canonical(row), status, reason),
            )
            row_id = cur2.lastrowid
            if status == "CONFLICT":
                self.conn.execute(
                    "INSERT INTO review_queue(ref_type, ref_id, reason,"
                    " created_by, created_at) VALUES ('IMPORT_ROW',?,?,?,?)",
                    (row_id, reason, username, _now()),
                )
            elif status == "NEW":
                self._apply_import_row(user, period_code, row)
                self.conn.execute(
                    "UPDATE import_rows SET status='APPLIED' WHERE id=?",
                    (row_id,),
                )
                applied += 1
        final_status = "CONFLICT_REVIEW" if conflicts else "DONE"
        self.conn.execute(
            "UPDATE import_batches SET status=?, applied_rows=? WHERE id=?",
            (final_status, applied, batch_id),
        )
        self._audit(username, "IMPORT", "import_batch", batch_id,
                    {"applied": applied, "duplicates": duplicates,
                     "conflicts": conflicts})
        return {"batch_id": batch_id, "status": final_status,
                "applied": applied, "conflicts": conflicts,
                "duplicates": duplicates}

    def _validate_import_row(self, user: sqlite3.Row, period_code: str,
                             row: dict[str, Any]) -> None:
        kind = row.get("row_kind")
        if kind not in ROW_KINDS:
            raise ValidationError(f"未知导入类型：{kind!r}")
        if not str(row.get("line_ref", "")).strip():
            raise ValidationError("导入行缺少来源凭证号 line_ref")
        org = row.get("reported_org") or row.get("seller_org")
        if not org:
            raise ValidationError("导入行缺少报送单位")
        self._assert_unit_scope(user, org)
        if not row.get("project_code") and kind != "PUBLIC_SERVICE":
            raise ValidationError(f"{kind}行缺少项目编码")

    def _apply_import_row(self, user: sqlite3.Row, period_code: str,
                          row: dict[str, Any], _allow_closed: bool = False) -> int:
        kind = row["row_kind"]
        username = user["username"]
        if kind == "OUTPUT":
            return self.report_output(
                username, period_code, row["project_code"],
                row["revenue"], row["value_added"],
                claimed_strategic=bool(row.get("claimed_strategic", False)),
                evidence_ref=row.get("evidence_ref", ""),
                task_code=row.get("task_code"), batch_code=row.get("batch_code"),
                _allow_closed=_allow_closed)
        if kind == "RD_EXPENSE":
            return self.report_rd_expense(
                username, period_code, row["project_code"], row["amount"],
                basic_claim=bool(row.get("basic_claim", False)),
                evidence_ref=row.get("evidence_ref", ""),
                task_code=row.get("task_code"),
                batch_code=row.get("batch_code"),
                _allow_closed=_allow_closed)
        if kind == "TRANSFORMATION":
            return self.report_transformation(
                username, period_code, row["project_code"],
                row["revenue"], row["value_added"],
                evidence_ref=row.get("evidence_ref", ""),
                batch_code=row.get("batch_code"),
                _allow_closed=_allow_closed)
        if kind == "PUBLIC_SERVICE":
            return self.report_public_service(
                username, period_code, row["reported_org"], row["amount"],
                metric_text=row.get("metric_text", ""),
                evidence_ref=row.get("evidence_ref", ""),
                project_code=row.get("project_code"),
                _allow_closed=_allow_closed)
        if kind == "INTERNAL_TRADE":
            return self.record_internal_trade(
                username, period_code, row["seller_org"], row["buyer_org"],
                row["revenue_amount"], row["value_added_amount"],
                project_code=row.get("project_code"),
                _allow_closed=_allow_closed)
        raise ValidationError(f"未知导入类型：{kind}")

    def list_open_reviews(self, username: str) -> list[dict[str, Any]]:
        self._require_role(username, {ROLE_REVIEWER, ROLE_DATA_OFFICE})
        rows = self.conn.execute(
            "SELECT * FROM review_queue WHERE status='OPEN' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_import_conflict(self, username: str, import_row_id: int,
                                accept: bool, note: str = "") -> None:
        """复核人员裁决冲突行：接受则按材料落库，驳回则作废。"""
        self._require_role(username, {ROLE_REVIEWER})
        row = self.conn.execute(
            "SELECT ir.*, ib.period_code AS period_code, ib.submitted_by AS"
            " submitted_by FROM import_rows ir JOIN import_batches ib"
            " ON ir.batch_id=ib.id WHERE ir.id=?",
            (import_row_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"导入行不存在：{import_row_id}")
        if row["status"] != "CONFLICT":
            raise InvalidStateError("仅冲突中的材料可以裁决")
        if self._period(row["period_code"])["status"] == "CLOSED":
            raise PeriodClosedError(
                "报告期已关账，冲突材料不得直接落库；请通过更正版本处理"
            )
        payload = json.loads(row["payload"])
        if accept:
            submitter = self._user(row["submitted_by"])
            record_id = self._apply_import_row(
                submitter, row["period_code"], payload, _allow_closed=False
            )
            new_status = "APPLIED"
            detail = f"接受冲突材料，落库记录{record_id}"
        else:
            new_status = "REJECTED"
            detail = "驳回冲突材料"
        self.conn.execute(
            "UPDATE import_rows SET status=?, reviewed_by=?, reviewed_at=?"
            " WHERE id=?",
            (new_status, username, _now(), import_row_id),
        )
        self.conn.execute(
            "UPDATE review_queue SET status='RESOLVED', resolved_by=?,"
            " resolved_at=?, resolution=? WHERE ref_type='IMPORT_ROW'"
            " AND ref_id=? AND status='OPEN'",
            (username, _now(), f"{detail}：{note}", import_row_id),
        )
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS c FROM import_rows WHERE batch_id=?"
            " AND status='CONFLICT'",
            (row["batch_id"],),
        ).fetchone()["c"]
        if remaining == 0:
            self.conn.execute(
                "UPDATE import_batches SET status='DONE' WHERE id=?",
                (row["batch_id"],),
            )
        self._audit(username, "RESOLVE_IMPORT", "import_row",
                    import_row_id, detail)

    # -- 数据集装配与报告 --------------------------------------------------
    def build_dataset(self, period_code: str) -> dict[str, Any]:
        period = self._period(period_code)

        def rows(sql: str, *params: Any) -> list[dict[str, Any]]:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

        industries = rows("SELECT * FROM industry_catalog WHERE active=1")
        projects = rows("SELECT * FROM projects")
        rule_sets = rows(
            "SELECT * FROM allocation_rule_sets WHERE active=1"
            " AND period_code=?", period_code
        )
        for rule in rule_sets:
            rule["shares"] = rows(
                "SELECT org_code, share FROM allocation_shares"
                " WHERE rule_set_id=?", rule["id"]
            )
        approved: dict[tuple[str, str], dict[str, Any]] = {}
        approval_log = []
        for req in rows(
            "SELECT * FROM classification_requests WHERE status='APPROVED'"
            " AND period_code=?", period_code
        ):
            approved[(req["kind"], str(req["target_ref"]))] = req
            approval_log.append(
                {
                    "request_id": req["id"],
                    "kind": req["kind"],
                    "target_ref": req["target_ref"],
                    "claimed_value": req["claimed_value"],
                    "submitted_by": req["submitted_by"],
                    "submitted_org": req["submitted_org"],
                    "reviewed_by": req["reviewed_by"],
                    "reviewed_at": req["reviewed_at"],
                    "evidence_ref": req["evidence_ref"],
                }
            )

        outputs = rows("SELECT * FROM outputs WHERE period_code=?", period_code)
        for line in outputs:
            line["strategic"] = (
                line["claimed_strategic"]
                and ("OUTPUT_STRATEGIC", str(line["id"])) in approved
            )
        rd_expenses = rows(
            "SELECT * FROM rd_expenses WHERE period_code=?", period_code
        )
        for line in rd_expenses:
            line["basic"] = (
                line["basic_claim"]
                and ("RD_BASIC", str(line["id"])) in approved
            )
        transformations = rows(
            "SELECT * FROM transformations WHERE period_code=?", period_code
        )
        public_services = rows(
            "SELECT * FROM public_services WHERE period_code=?", period_code
        )
        for line in public_services:
            # 公共服务按统一分摊规则归集（若项目存在规则）
            line["reported_org"] = line["org_code"]
        internal_trades = rows(
            "SELECT * FROM internal_trades WHERE period_code=?", period_code
        )
        goals = rows("SELECT * FROM period_goals WHERE period_code=?",
                     period_code)

        dataset = {
            "formula_version": engine.FORMULA_VERSION,
            "period": {"code": period["code"], "name": period["name"],
                       "status": period["status"]},
            "organizations": rows(
                "SELECT code, name, org_type, parent_code FROM organizations"
            ),
            "industries": industries,
            "projects": projects,
            "project_splits": rows(
                "SELECT * FROM project_splits WHERE effective_period=?",
                period_code
            ),
            "rules": rule_sets,
            "reorg_events": rows(
                "SELECT * FROM reorg_events WHERE effective_period=?",
                period_code
            ),
            "reorg_mappings": rows(
                "SELECT rm.* FROM reorg_mappings rm JOIN reorg_events re"
                " ON rm.event_id=re.id WHERE re.effective_period=?",
                period_code
            ),
            "outputs": outputs,
            "rd_expenses": rd_expenses,
            "transformations": transformations,
            "public_services": public_services,
            "internal_trades": internal_trades,
            "goals": goals,
            "approval_log": approval_log,
        }
        prior_code = period["prior_period_code"]
        if prior_code:
            prior_report = self.latest_snapshot_report(prior_code)
            if prior_report is not None:
                dataset["prior_totals"] = {
                    "rd_total": prior_report["metrics"]["basic_research_ratio"][
                        "rd_total"
                    ],
                }
                dataset["prior_metrics"] = {
                    "strategic_va_ratio": prior_report["metrics"][
                        "strategic_va_ratio"
                    ]["ratio"],
                    "basic_research_ratio": prior_report["metrics"][
                        "basic_research_ratio"
                    ]["ratio"],
                }
        return dataset

    def management_report(self, period_code: str) -> dict[str, Any]:
        """管理层报告：关账后一律读取冻结快照，开放期实时计算。"""
        period = self._period(period_code)
        if period["status"] == "CLOSED":
            report = self.latest_snapshot_report(period_code)
            if report is None:
                raise InvalidStateError("已关账期间缺少快照")
            report["frozen"] = True
            return report
        dataset = self.build_dataset(period_code)
        report = engine.compute_report(dataset)
        report["frozen"] = False
        return report

    def latest_snapshot_report(self, period_code: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM snapshots WHERE period_code=?"
            " ORDER BY revision DESC LIMIT 1",
            (period_code,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])["report"]

    def get_snapshot(self, period_code: str, revision: int) -> dict[str, Any]:
        """读取指定修订版本的冻结快照（关账原版为 revision=0）。"""
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE period_code=? AND revision=?",
            (period_code, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"快照不存在：{period_code}#r{revision}")
        return dict(row) | {"payload": json.loads(row["payload"])}

    # -- 关账快照 ----------------------------------------------------------
    def start_close(self, username: str, period_code: str) -> int:
        """发起关账：创建持久化作业。进程重启后可用 resume_jobs 续跑。"""
        self._require_role(username, {ROLE_DATA_OFFICE})
        period = self._period(period_code)
        if period["status"] == "CLOSED":
            raise PeriodClosedError(f"报告期{period_code}已关账")
        existing = self.conn.execute(
            "SELECT id FROM jobs WHERE kind='CLOSE_PERIOD' AND period_code=?"
            " AND status IN ('PENDING','RUNNING','BLOCKED')",
            (period_code,),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        cur = self.conn.execute(
            "INSERT INTO jobs(kind, period_code, status, created_at, updated_at)"
            " VALUES ('CLOSE_PERIOD',?,'PENDING',?,?)",
            (period_code, _now(), _now()),
        )
        self._audit(username, "START_CLOSE", "period", period_code)
        return cur.lastrowid

    def execute_close_job(self, job_id: int) -> dict[str, Any]:
        """执行关账作业：前置检查不通过则保持 BLOCKED 并生成催补清单。

        关账成功时把组织范围、产业目录、目标、全部贡献记录与公式版本
        连同计算结果整体写入快照（revision=0），随后期间转为 CLOSED。
        """
        job = self.conn.execute("SELECT * FROM jobs WHERE id=?",
                                (job_id,)).fetchone()
        if job is None:
            raise NotFoundError(f"作业不存在：{job_id}")
        if job["status"] == "DONE":
            return {"job_id": job_id, "status": "DONE"}
        period_code = job["period_code"]
        self.conn.execute(
            "UPDATE jobs SET status='RUNNING', attempts=attempts+1,"
            " updated_at=? WHERE id=?",
            (_now(), job_id),
        )
        dataset = self.build_dataset(period_code)
        blockers = engine.find_blockers(dataset)
        if blockers:
            messages = [b["message"] for b in blockers]
            self.conn.execute(
                "UPDATE jobs SET status='BLOCKED', last_error=?, updated_at=?"
                " WHERE id=?",
                ("；".join(messages), _now(), job_id),
            )
            # 阻断项转成持久化催补，重启后不丢失
            for blocker in blockers:
                self.create_reminder_if_absent(
                    period_code,
                    blocker["target_org"],
                    blocker["message"],
                    "关账前置检查未通过",
                )
            self.conn.commit()
            self._audit("SYSTEM", "BLOCK_CLOSE", "job", job_id,
                        {"blockers": messages})
            return {"job_id": job_id, "status": "BLOCKED",
                    "blockers": messages}
        report = engine.compute_report(dataset)
        snapshot_payload = engine.to_jsonable(
            {"dataset": dataset, "report": report}
        )
        fingerprint = engine.dataset_fingerprint(snapshot_payload)
        self.conn.execute(
            "INSERT INTO snapshots(period_code, revision, formula_version,"
            " fingerprint, payload, kind, taken_at)"
            " VALUES (?,0,?,?,?,'CLOSE',?)",
            (period_code, engine.FORMULA_VERSION, fingerprint,
             snapshot_payload, _now()),
        )
        self.conn.execute(
            "UPDATE periods SET status='CLOSED', closed_at=?, closed_by='SYSTEM'"
            " WHERE code=?",
            (_now(), period_code),
        )
        # 前置条件全部满足，自动核销该期遗留催补
        self.conn.execute(
            "UPDATE reminders SET status='RESOLVED', resolved_by='SYSTEM',"
            " resolved_at=? WHERE period_code=? AND status='PENDING'",
            (_now(), period_code),
        )
        self.conn.execute(
            "UPDATE jobs SET status='DONE', updated_at=? WHERE id=?",
            (_now(), job_id),
        )
        self._audit("SYSTEM", "CLOSE_PERIOD", "period", period_code,
                    {"formula_version": engine.FORMULA_VERSION,
                     "fingerprint": fingerprint})
        self.conn.commit()
        return {"job_id": job_id, "status": "DONE",
                "fingerprint": fingerprint}

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- 更正版本 ----------------------------------------------------------
    def submit_correction(self, username: str, period_code: str,
                          reason: str,
                          entries: list[dict[str, Any]]) -> int:
        """历史错误只能通过更正版本处理。

        每个条目含 section / target_id / before / after；公式版本与组织
        范围不得修改，且更正必须经复核人员（与提交人不同）批准后才生效。
        """
        self._require_role(username, {ROLE_DATA_OFFICE})
        period = self._period(period_code)
        if period["status"] != "CLOSED":
            raise InvalidStateError("只有已关账期间需要走更正版本，"
                                    "开放期间请直接订正")
        if not entries:
            raise ValidationError("更正至少包含一个条目")
        validated: list[tuple[str, str, str, str]] = []
        for entry in entries:
            section = entry["section"]
            if section not in CORRECTABLE_SECTIONS:
                raise ValidationError(
                    f"更正不允许修改{section}（公式与组织范围冻结）"
            )
            allowed = CORRECTION_ALLOWED_COLUMNS[section]
            illegal = set(entry["after"]) - allowed
            if illegal:
                raise ValidationError(
                    f"更正不得修改冻结字段：{','.join(sorted(illegal))}"
                    "（报送单位、报告期与审批留痕不可改）"
                )
            target_id = str(entry["target_id"])
            current = self.conn.execute(
                f"SELECT * FROM {section} WHERE id=?", (target_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError(f"{section}中不存在记录{target_id}")
            before = {k: current[k] for k in current.keys()}
            after = dict(before)
            after.update(entry["after"])
            if after.get("period_code") not in (None, period_code):
                raise ValidationError("更正不得把记录移出原报告期")
            validated.append(
                (section, target_id, engine.to_jsonable(before),
                 engine.to_jsonable(after))
            )
        revision = self.conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 AS r FROM snapshots"
            " WHERE period_code=?", (period_code,),
        ).fetchone()["r"]
        cur = self.conn.execute(
            "INSERT INTO corrections(period_code, revision, reason, status,"
            " submitted_by, submitted_at) VALUES (?,?,?,'SUBMITTED',?,?)",
            (period_code, revision, reason, username, _now()),
        )
        correction_id = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO correction_entries(correction_id, section,"
            " target_id, before_json, after_json) VALUES (?,?,?,?,?)",
            [(correction_id, *v) for v in validated],
        )
        self._audit(username, "SUBMIT_CORRECTION", "correction",
                    correction_id, {"reason": reason, "entries": len(entries)})
        return correction_id

    def review_correction(self, username: str, correction_id: int,
                          approve: bool, note: str = "") -> int | None:
        """复核人员批准/驳回更正；批准即应用并重算快照（公式版本不变）。"""
        self._require_role(username, {ROLE_REVIEWER})
        correction = self.conn.execute(
            "SELECT * FROM corrections WHERE id=?", (correction_id,)
        ).fetchone()
        if correction is None:
            raise NotFoundError(f"更正版本不存在：{correction_id}")
        if correction["status"] != "SUBMITTED":
            raise InvalidStateError("该更正版本已复核")
        if correction["submitted_by"] == username:
            raise PermissionDeniedError("不得批准自己提交的更正版本")
        # 先把已积累的修改落盘，使下面被拒回滚只撤销本更正的数据变更
        self.conn.commit()
        if not approve:
            self.conn.execute(
                "UPDATE corrections SET status='REJECTED', reviewed_by=?,"
                " reviewed_at=? WHERE id=?",
                (username, _now(), correction_id),
            )
            self._audit(username, "REJECT_CORRECTION", "correction",
                        correction_id, note)
            return None
        period_code = correction["period_code"]
        entries = self.conn.execute(
            "SELECT * FROM correction_entries WHERE correction_id=?",
            (correction_id,),
        ).fetchall()
        for entry in entries:
            before = json.loads(entry["before_json"])
            after = json.loads(entry["after_json"])
            changed = {
                key: value for key, value in after.items()
                if key != "id" and before.get(key) != value
            }
            unknown = set(changed) - CORRECTION_ALLOWED_COLUMNS[entry["section"]]
            if unknown:
                # 双保险：提交时已校验，批准时再拦一次
                raise PermissionDeniedError(
                    f"更正含冻结字段：{','.join(sorted(unknown))}"
                )
            if changed:
                self.conn.execute(
                    f"UPDATE {entry['section']} SET "
                    + ", ".join(f"{k}=?" for k in changed)
                    + " WHERE id=?",
                    [*changed.values(), entry["target_id"]],
                )
        # 用同一公式版本重算，形成新的冻结快照修订
        dataset = self.build_dataset(period_code)
        blockers = engine.find_blockers(dataset)
        if blockers:
            # 回滚已应用的修改：更正不得把已关账期间改回不可关账状态
            self.conn.rollback()
            self.conn.execute(
                "UPDATE corrections SET status='REJECTED', reviewed_by=?,"
                " reviewed_at=? WHERE id=?",
                (username, _now(), correction_id),
            )
            self.conn.commit()
            raise InvalidStateError(
                "更正后存在未决阻断（如特殊归类缺证据），不能应用："
                + "；".join(b["message"] for b in blockers)
            )
        report = engine.compute_report(dataset)
        snapshot_payload = engine.to_jsonable(
            {"dataset": dataset, "report": report}
        )
        fingerprint = engine.dataset_fingerprint(snapshot_payload)
        self.conn.execute(
            "INSERT INTO snapshots(period_code, revision, formula_version,"
            " fingerprint, payload, kind, correction_id, taken_at)"
            " VALUES (?,?,?,?,?,'CORRECTION',?,?)",
            (period_code, correction["revision"],
             engine.FORMULA_VERSION, fingerprint, snapshot_payload,
             correction_id, _now()),
        )
        self.conn.execute(
            "UPDATE corrections SET status='APPLIED', reviewed_by=?,"
            " reviewed_at=?, applied_at=? WHERE id=?",
            (username, _now(), _now(), correction_id),
        )
        self._audit(username, "APPLY_CORRECTION", "correction",
                    correction_id,
                    {"revision": correction["revision"],
                     "fingerprint": fingerprint})
        return correction["revision"]

    # -- 催补 --------------------------------------------------------------
    def create_reminder(self, username: str, period_code: str,
                        target_org: str, entity_desc: str, reason: str) -> int:
        self._require_role(username, {ROLE_DATA_OFFICE, ROLE_REVIEWER})
        return self._insert_reminder(period_code, target_org, entity_desc,
                                     reason, username)

    def create_reminder_if_absent(self, period_code: str, target_org: str,
                                  entity_desc: str, reason: str) -> int | None:
        """供关账作业与重启扫描调用；同一缺口只保留一条待办。"""
        exists = self.conn.execute(
            "SELECT id FROM reminders WHERE period_code=? AND entity_desc=?"
            " AND status='PENDING'",
            (period_code, entity_desc),
        ).fetchone()
        if exists is not None:
            return None
        return self._insert_reminder(period_code, target_org, entity_desc,
                                     reason, "SYSTEM")

    def _insert_reminder(self, period_code: str, target_org: str,
                         entity_desc: str, reason: str, actor: str) -> int:
        self._assert_org_exists(target_org)
        cur = self.conn.execute(
            "INSERT INTO reminders(period_code, target_org, entity_desc,"
            " reason, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (period_code, target_org, entity_desc, reason, actor, _now()),
        )
        return cur.lastrowid

    def list_pending_reminders(self, period_code: str | None = None
                               ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reminders WHERE status='PENDING'"
        params: tuple[Any, ...] = ()
        if period_code:
            sql += " AND period_code=?"
            params = (period_code,)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def resolve_reminder(self, username: str, reminder_id: int) -> None:
        if username != "SYSTEM":
            self._user(username)
        self.conn.execute(
            "UPDATE reminders SET status='RESOLVED', resolved_by=?,"
            " resolved_at=? WHERE id=?",
            (username, _now(), reminder_id),
        )

    def commit(self) -> None:
        self.conn.commit()
