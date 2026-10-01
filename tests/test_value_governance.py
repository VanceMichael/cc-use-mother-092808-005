"""价值贡献治理后端测试：归集、抵销、审批、关账、更正、重启、报告。"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.value_governance import (
    GovernanceService,
    PeriodClosedError,
    PermissionDeniedError,
    ValidationError,
    resume_jobs,
    sweep_reminders,
)
from src.value_governance import engine

from tests.vg_world import build_world, seed_2030, seed_prior_period


def money(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_world()
        self.ids = seed_2030(self.svc)

    def test_joint_project_attributed_by_approved_shares(self) -> None:
        """三方各报同一项目时，按批准份额归一，集团总额不重复计算。"""
        report = self.svc.management_report("2030")
        project = next(p for p in report["projects"]
                       if p["project_code"] == "P1")
        # 毛额 = 三方申报之和（1000 增加值 / 2000 收入）
        self.assertEqual(
            sum((Decimal(v) for fields in project["gross_by_reporter"].values()
                 for k, v in fields.items() if k.endswith("value_added")),
                Decimal("0")),
            money("1000.00"),
        )
        allocated = project["allocated_to_org"]
        self.assertEqual(
            Decimal(allocated["SE1"]["outputs.value_added"]), money("500.00"))
        self.assertEqual(
            Decimal(allocated["RI1"]["outputs.value_added"]), money("300.00"))
        self.assertEqual(
            Decimal(allocated["PS1"]["outputs.value_added"]), money("200.00"))
        # 分文不差
        self.assertEqual(
            sum((Decimal(v) for fields in allocated.values()
                 for k, v in fields.items()
                 if k == "outputs.value_added"), Decimal("0")),
            money("1000.00"),
        )
        self.assertEqual(project["rule_set_ids"], [1])

    def test_claim_structure_mismatch_blocks_close(self) -> None:
        """申报构成与批准份额不符（各报全额式重复）必须阻断关账。"""
        svc = build_world()
        svc.set_goal("planner", "2030", "STRATEGIC_VA_RATIO", "0.20")
        svc.define_allocation(
            "office", "2030", "P1", "CONTRACT",
            {"SE1": "0.5", "RI1": "0.3", "PS1": "0.2"},
        )
        # 三方都按全额申报，申报构成 1/3、1/3、1/3
        svc.report_output("u_se", "2030", "P1", "1000", "1000")
        svc.report_output("u_ri", "2030", "P1", "1000", "1000")
        svc.report_output("u_ps", "2030", "P1", "1000", "1000")
        job = svc.start_close("office", "2030")
        result = svc.execute_close_job(job)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("申报构成" in b for b in result["blockers"]))

    def test_missing_rule_for_multi_unit_scope_blocks_close(self) -> None:
        # 模拟历史数据缺口：同一作用域存在两个报送单位但没有分摊规则
        svc = build_world()
        for org in ("SE1", "RI1"):
            svc.conn.execute(
                "INSERT INTO outputs(period_code, project_code, revenue,"
                " value_added, claimed_strategic, reported_by, reported_org,"
                " created_at) VALUES ('2030','P1','500','500',0,'u_se',"
                "?, 't')",
                (org,),
            )
        job = svc.start_close("office", "2030")
        result = svc.execute_close_job(job)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("缺少分摊规则" in b for b in result["blockers"]))

    def test_shares_must_sum_to_one(self) -> None:
        with self.assertRaisesRegex(ValidationError, "份额合计"):
            self.svc.define_allocation(
                "office", "2030", "P1", "CONTRACT",
                {"SE1": "0.5", "RI1": "0.3"},
            )

    def test_unit_cannot_define_rules_or_report_for_others(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.svc.define_allocation(
                "u_ri", "2030", "P1", "CONTRACT", {"SE1": "0.5", "RI1": "0.5"})
        with self.assertRaises(PermissionDeniedError):
            self.svc.report_output("u_ri", "2030", "P2", "100", "100")


class InternalTradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_world()
        self.ids = seed_2030(self.svc)

    def test_only_matched_trades_eliminated(self) -> None:
        report = self.svc.management_report("2030")
        m = report["metrics"]["strategic_va_ratio"]
        # 毛增加值 3000（P1 1000 + P2 2000），抵销战新项目内部交易 100
        self.assertEqual(m["gross_value_added"], "3000.00")
        self.assertEqual(m["eliminated_value_added"], "100.00")
        self.assertEqual(m["net_value_added"], "2900.00")
        self.assertEqual(
            m["gross_strategic_value_added"], "1000.00")
        self.assertEqual(
            m["eliminated_strategic_value_added"], "100.00")
        self.assertEqual(m["net_strategic_value_added"], "900.00")
        self.assertEqual(report["metrics"]["net_revenue"], "6800.00")
        elimination = report["eliminations"][0]
        self.assertEqual(elimination["seller_org"], "SE1")
        self.assertEqual(elimination["buyer_org"], "RI1")
        self.assertTrue(elimination["strategic"])
        self.assertEqual(elimination["confirmed_by"], "u_ri")

    def test_pending_trade_blocks_close(self) -> None:
        svc = build_world()
        seed_2030(svc)
        svc.record_internal_trade("u_se", "2030", "SE1", "PS1", "50", "20",
                                  project_code="P2")
        result = svc.execute_close_job(svc.start_close("office", "2030"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("未经双边确认" in b for b in result["blockers"]))

    def test_dispute_resolved_by_reviewer_only(self) -> None:
        svc = build_world()
        ids = seed_2030(svc)
        svc.dispute_internal_trade("u_ri", ids["trade"], "金额有误")
        with self.assertRaises(PermissionDeniedError):
            svc.resolve_trade_dispute("u_ri", ids["trade"], "自行处理", True)
        svc.resolve_trade_dispute("rev", ids["trade"], "复核后确认", True)
        result = svc.execute_close_job(svc.start_close("office", "2030"))
        self.assertEqual(result["status"], "DONE")


class ClassificationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_world()

    def test_submitter_cannot_approve_own_classification(self) -> None:
        seed_2030(self.svc)
        # u_ri 提交的基础研究认定，u_ri 自己批准被拒
        request = self.svc.conn.execute(
            "SELECT id FROM classification_requests"
            " WHERE submitted_by='u_ri' ORDER BY id"
        ).fetchone()["id"]
        # 已由 rev 批准过；再造一笔由 u_ri 提交、尝试自批
        new_id = self.svc.report_rd_expense(
            "u_ri", "2030", "P1", "5", basic_claim=True, evidence_ref="EV-X")
        req = self.svc.submit_classification(
            "u_ri", "2030", "RD_BASIC", str(new_id), "true", "EV-X")
        with self.assertRaises(PermissionDeniedError):
            self.svc.review_classification("u_ri", req, True)
        # 复核员也不能批准别人代提交的申请：自批禁令以提交人为准
        rid = self.svc.report_rd_expense(
            "u_ri", "2030", "P1", "6", basic_claim=True, evidence_ref="EV-Y")
        req2 = self.svc.submit_classification(
            "u_ri", "2030", "RD_BASIC", str(rid), "true", "EV-Y")
        with self.assertRaises(PermissionDeniedError):
            self.svc.review_classification("u_ri", req2, True)

    def test_unapproved_basic_claim_not_counted_and_blocks_close(self) -> None:
        seed_2030(self.svc)
        # 新增一笔只申请未批准的基础研究费用
        self.svc.report_rd_expense(
            "u_ri", "2030", "P1", "10", basic_claim=True, evidence_ref="EV-Z")
        report = self.svc.management_report("2030")
        # 基础研究投入仍只计已批准的 30
        self.assertEqual(
            report["metrics"]["basic_research_ratio"]["basic_total"], "30.00")
        result = self.svc.execute_close_job(
            self.svc.start_close("office", "2030"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("基础研究属性" in b for b in result["blockers"]))

    def test_project_industry_reclassification_changes_totals(self) -> None:
        seed_2030(self.svc)
        req = self.svc.submit_classification(
            "u_se", "2030", "PROJECT_INDUSTRY", "P2", "I_STR", "EV-IND-1")
        # 业务单位自批被拒
        with self.assertRaises(PermissionDeniedError):
            self.svc.review_classification("u_se", req, True)
        self.svc.review_classification("rev", req, True, "符合战新目录")
        report = self.svc.management_report("2030")
        # P2 重分类为战新后，净战新增加值 = 1000 + 2000 - 100
        self.assertEqual(
            report["metrics"]["strategic_va_ratio"][
                "net_strategic_value_added"], "2900.00")
        approvals = {a["kind"] for a in report["approvals"]}
        self.assertIn("PROJECT_INDUSTRY", approvals)


class ImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_world()
        seed_2030(self.svc)

    def _rows(self, amount: str = "100") -> list[dict]:
        return [
            {
                "row_kind": "RD_EXPENSE",
                "line_ref": "IMP-001",
                "reported_org": "SE1",
                "project_code": "P2",
                "amount": amount,
            }
        ]

    def test_exact_resend_is_idempotent(self) -> None:
        first = self.svc.submit_import_batch(
            "u_se", "2030", "BATCH-A", self._rows("100"))
        self.assertEqual(first["status"], "DONE")
        self.assertEqual(first["applied"], 1)
        again = self.svc.submit_import_batch(
            "u_se", "2030", "BATCH-A", self._rows("100"))
        self.assertEqual(again["status"], "RESEND")
        self.assertEqual(again["duplicates"], 1)
        rows = self.svc.conn.execute(
            "SELECT COUNT(*) AS c FROM rd_expenses WHERE amount='100'"
        ).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_same_ref_different_payload_is_rejected(self) -> None:
        self.svc.submit_import_batch("u_se", "2030", "BATCH-B", self._rows())
        with self.assertRaisesRegex(Exception, "不同内容"):
            self.svc.submit_import_batch(
                "u_se", "2030", "BATCH-B", self._rows("120"))

    def test_same_voucher_different_content_goes_to_review(self) -> None:
        self.svc.submit_import_batch(
            "u_se", "2030", "BATCH-C",
            self._rows("100"))
        result = self.svc.submit_import_batch(
            "u_se", "2030", "BATCH-D",
            self._rows("120"))
        self.assertEqual(result["status"], "CONFLICT_REVIEW")
        self.assertEqual(result["conflicts"], 1)
        # 冲突行未落库
        self.assertEqual(
            self.svc.conn.execute(
                "SELECT COUNT(*) AS c FROM rd_expenses WHERE amount='120'"
            ).fetchone()["c"], 0)
        queued = self.svc.list_open_reviews("rev")
        self.assertEqual(len(queued), 1)
        row_id = self.svc.conn.execute(
            "SELECT id FROM import_rows WHERE status='CONFLICT'"
        ).fetchone()["id"]
        # 业务单位不能裁决
        with self.assertRaises(PermissionDeniedError):
            self.svc.resolve_import_conflict("u_se", row_id, True)
        self.svc.resolve_import_conflict("rev", row_id, True, "凭证属实")
        self.assertEqual(
            self.svc.conn.execute(
                "SELECT COUNT(*) AS c FROM rd_expenses WHERE amount='120'"
            ).fetchone()["c"], 1)


class CloseAndCorrectionTest(unittest.TestCase):
    def test_close_freezes_formula_org_scope_and_data(self) -> None:
        svc = build_world()
        seed_2030(svc)
        result = svc.execute_close_job(svc.start_close("office", "2030"))
        self.assertEqual(result["status"], "DONE")
        # 公式版本随快照冻结
        snap = svc.get_snapshot("2030", 0)
        self.assertEqual(snap["formula_version"], engine.FORMULA_VERSION)
        self.assertEqual(snap["kind"], "CLOSE")
        # 关账后：目标、主数据、报送、规则全部冻结
        with self.assertRaises(PeriodClosedError):
            svc.set_goal("planner", "2030", "STRATEGIC_VA_RATIO", "0.99")
        with self.assertRaises(PeriodClosedError):
            svc.report_output("u_se", "2030", "P2", "1", "1")
        with self.assertRaises(PeriodClosedError):
            svc.define_allocation(
                "office", "2030", "P2", "CONTRACT", {"SE1": "1"})
        with self.assertRaises(PeriodClosedError):
            svc.create_investment_batch(
                "office", "B9", "P2", "2030", "批次", "10")
        # 管理层报告读快照
        report = svc.management_report("2030")
        self.assertTrue(report["frozen"])

    def test_historical_error_fixed_by_correction_version(self) -> None:
        svc = build_world()
        ids = seed_2030(svc)
        svc.execute_close_job(svc.start_close("office", "2030"))
        before = svc.management_report("2030")
        va_before = Decimal(
            before["metrics"]["strategic_va_ratio"]["net_value_added"])
        # 发现 P1 公共服务单位多报 10（其份额行原值 200 → 190）
        correction = svc.submit_correction(
            "office", "2030", "公共服务单位多计10元增加值",
            [{"section": "outputs", "target_id": str(ids["out_ps"]),
              "after": {"value_added": "190"}}],
        )
        # 提交人（数据办）不能批准自己的更正
        with self.assertRaises(PermissionDeniedError):
            svc.review_correction("office", correction, True)
        revision = svc.review_correction("rev", correction, True, "核实")
        self.assertEqual(revision, 1)
        after = svc.management_report("2030")
        va_after = Decimal(
            after["metrics"]["strategic_va_ratio"]["net_value_added"])
        self.assertEqual(va_before - va_after, money("10.00"))
        # 原快照保留，公式版本不变
        original = svc.get_snapshot("2030", 0)
        revised = svc.get_snapshot("2030", 1)
        self.assertEqual(original["formula_version"],
                         revised["formula_version"])
        self.assertEqual(revised["kind"], "CORRECTION")
        # 更正不能碰公式/组织范围
        with self.assertRaisesRegex(ValidationError, "冻结"):
            svc.submit_correction(
                "office", "2030", "试图改规则",
                [{"section": "allocation_rule_sets", "target_id": "1",
                  "after": {"basis": "CUSTOM"}}])

    def test_correction_cannot_change_reporter_or_period(self) -> None:
        svc = build_world()
        ids = seed_2030(svc)
        svc.execute_close_job(svc.start_close("office", "2030"))
        with self.assertRaisesRegex(ValidationError, "冻结字段"):
            svc.submit_correction(
                "office", "2030", "试图把贡献划给自己",
                [{"section": "outputs", "target_id": str(ids["out_ps"]),
                  "after": {"reported_org": "SE1", "period_code": "2031"}}])

    def test_correction_introducing_unapproved_claim_is_rejected(self) -> None:
        svc = build_world()
        ids = seed_2030(svc)
        svc.execute_close_job(svc.start_close("office", "2030"))
        # 借更正把一笔产出标记为战新申请，却没有获批证据 → 不得应用
        cid = svc.submit_correction(
            "office", "2030", "补提战新认定",
            [{"section": "outputs", "target_id": str(ids["out_se"]),
              "after": {"claimed_strategic": 1}}],
        )
        with self.assertRaisesRegex(Exception, "未决阻断"):
            svc.review_correction("rev", cid, True)
        status = svc.conn.execute(
            "SELECT status FROM corrections WHERE id=?", (cid,)
        ).fetchone()["status"]
        self.assertEqual(status, "REJECTED")

    def test_open_period_goal_change_does_not_touch_history(self) -> None:
        svc = build_world()
        seed_prior_period(svc)
        seed_2030(svc)
        svc.set_goal("planner", "2030", "BASIC_RESEARCH_RATIO", "0.50")
        report_2030 = svc.management_report("2030")
        self.assertEqual(
            report_2030["metrics"]["basic_research_ratio"]["vs_goal"][
                "target"], "0.500000")
        # 2029 已关账，其报告不受影响
        report_2029 = svc.management_report("2029")
        self.assertTrue(report_2029["frozen"])


class RestartPersistenceTest(unittest.TestCase):
    def test_unfinished_close_and_reminders_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "gov.db")
            svc = build_world(db_path)
            seed_2030(svc)
            # 新增一笔买方未确认的内部交易，关账必然阻断
            svc.record_internal_trade("u_se", "2030", "SE1", "PS1",
                                      "40", "20", project_code="P2")
            result = svc.execute_close_job(svc.start_close("office", "2030"))
            self.assertEqual(result["status"], "BLOCKED")
            reminders_before = len(svc.list_pending_reminders("2030"))
            self.assertGreaterEqual(reminders_before, 1)
            svc.commit()

            # 模拟进程重启：新连接、新服务实例，续跑作业
            svc2 = GovernanceService(db_path)
            results = resume_jobs(svc2)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "BLOCKED")
            # 催补仍在且不重复堆积
            self.assertEqual(
                len(svc2.list_pending_reminders("2030")), reminders_before)

            # 买方确认后再重启续跑，关账完成
            trade = svc2.conn.execute(
                "SELECT id FROM internal_trades WHERE buyer_org='PS1'"
            ).fetchone()["id"]
            svc2.create_user("u_ps_buyer", "公服买方", "UNIT_USER", "PS1")
            svc2.confirm_internal_trade("u_ps_buyer", trade)
            svc2.commit()
            svc3 = GovernanceService(db_path)
            results = resume_jobs(svc3)
            self.assertEqual(results[0]["status"], "DONE")
            # 关账完成时催补已自动核销
            self.assertEqual(svc3.list_pending_reminders("2030"), [])
            # 扫描再跑一遍也不重复堆积
            sweep = sweep_reminders(svc3)
            self.assertEqual(sweep["created"], 0)
            self.assertEqual(sweep["resolved"], 0)


class ReorgAndSplitTest(unittest.TestCase):
    def test_reorg_moves_contribution_without_changing_total(self) -> None:
        svc = build_world()
        seed_2030(svc)
        # 公共服务单位拆入两个新单位，权重 0.6/0.4
        svc.create_organization("office", "PS2", "公共服务二部",
                                "PUBLIC_SERVICE")
        svc.register_reorg(
            "office", "2030", "SPLIT", "公服单位分立",
            [("PS1", "PS2", "0.6"), ("PS1", "SE1", "0.4")],
        )
        report = svc.management_report("2030")
        project = next(p for p in report["projects"]
                       if p["project_code"] == "P1")
        allocated = project["allocated_to_org"]
        # PS1 的 200 增加值按 0.6/0.4 平移：120 / 80
        self.assertEqual(
            Decimal(allocated["PS2"]["outputs.value_added"]), money("120.00"))
        self.assertEqual(
            Decimal(allocated["SE1"]["outputs.value_added"]), money("580.00"))
        self.assertNotIn("PS1", allocated)
        # 集团总额不变
        self.assertEqual(
            report["metrics"]["strategic_va_ratio"][
                "gross_value_added"], "3000.00")

    def test_reorg_weights_must_sum_to_one(self) -> None:
        svc = build_world()
        seed_2030(svc)
        svc.create_organization("office", "PS2", "二部", "PUBLIC_SERVICE")
        with self.assertRaisesRegex(ValidationError, "权重合计"):
            svc.register_reorg(
                "office", "2030", "SPLIT", "x",
                [("PS1", "PS2", "0.6")])

    def test_split_project_rejects_parent_reporting(self) -> None:
        svc = build_world()
        svc.split_project(
            "office", "P1", "2030", [("P1A", "0.7"), ("P1B", "0.3")])
        with self.assertRaisesRegex(Exception, "已拆分"):
            svc.report_output("u_se", "2030", "P1", "100", "100")
        # 拆分权重必须合计为 1（用全新父项目 P2 触发权重校验）
        svc.create_project("office", "C1", "子项一", "SE1", "I_TRAD",
                           parent_project="P2")
        svc.create_project("office", "C2", "子项二", "SE1", "I_TRAD",
                           parent_project="P2")
        with self.assertRaisesRegex(ValidationError, "权重合计"):
            svc.split_project(
                "office", "P2", "2030", [("C1", "0.9"), ("C2", "0.3")])


class ManagementReportTest(unittest.TestCase):
    def test_report_carries_drilldown_and_true_gap(self) -> None:
        svc = build_world()
        seed_prior_period(svc)
        ids = seed_2030(svc)
        report = svc.management_report("2030")
        # 占比：净战新增加值 900 / 净增加值 2900 ≈ 0.310345
        ratio = Decimal(
            report["metrics"]["strategic_va_ratio"]["ratio"])
        self.assertAlmostEqual(ratio, Decimal("900") / Decimal("2900"),
                               places=5)
        # 距 20% 目标的真实差额（已超额，gap 为负）
        gap = report["metrics"]["strategic_va_ratio"]["vs_goal"]
        self.assertTrue(gap["achieved"])
        # 基础研究：30/100 = 30%，超过 15% 目标
        self.assertEqual(
            report["metrics"]["basic_research_ratio"]["ratio"], "0.300000")
        self.assertTrue(
            report["metrics"]["basic_research_ratio"]["vs_goal"]["achieved"])
        # 研发增速：(100-80)/80 = 25%，超过 7%
        self.assertEqual(report["metrics"]["rd_growth"]["actual"], "0.250000")
        self.assertTrue(report["metrics"]["rd_growth"]["achieved"])
        # 占比同比变化存在
        self.assertIsNotNone(
            report["metrics"]["strategic_va_ratio"]["ratio_change"])
        # 项目明细、抵销过程、审批责任齐备
        p1 = next(p for p in report["projects"] if p["project_code"] == "P1")
        self.assertTrue(p1["eliminations"])
        self.assertEqual(
            {a["reviewed_by"] for a in report["approvals"]}, {"rev"})
        self.assertEqual(
            {a["submitted_by"] for a in report["approvals"]}, {"u_ri"})

    def test_blockers_empty_when_clean(self) -> None:
        svc = build_world()
        seed_2030(svc)
        report = svc.management_report("2030")
        self.assertEqual(report["blockers"], [])

    def test_snapshot_replays_bit_for_bit(self) -> None:
        """冻结数据集用同一公式版本重算，必须与快照报告逐位一致。"""
        svc = build_world()
        seed_2030(svc)
        svc.execute_close_job(svc.start_close("office", "2030"))
        snap = svc.get_snapshot("2030", 0)
        replay = engine.compute_report(snap["payload"]["dataset"])
        self.assertEqual(
            engine.to_jsonable(replay["metrics"]),
            engine.to_jsonable(snap["payload"]["report"]["metrics"]),
        )

    def test_rejected_correction_leaves_history_untouched(self) -> None:
        svc = build_world()
        ids = seed_2030(svc)
        svc.execute_close_job(svc.start_close("office", "2030"))
        original_va = svc.management_report("2030")[
            "metrics"]["strategic_va_ratio"]["net_value_added"]
        correction = svc.submit_correction(
            "office", "2030", "证据不足的更正",
            [{"section": "outputs", "target_id": str(ids["out_ps"]),
              "after": {"value_added": "1"}}],
        )
        self.assertIsNone(svc.review_correction("rev", correction, False))
        self.assertEqual(
            svc.management_report("2030")["metrics"][
                "strategic_va_ratio"]["net_value_added"],
            original_va,
        )
        # 驳回后不产生新快照修订
        with self.assertRaises(Exception):
            svc.get_snapshot("2030", 1)

    def test_transformation_and_public_service_are_counted(self) -> None:
        svc = build_world()
        seed_2030(svc)
        svc.report_transformation("u_ri", "2030", "P1", "300", "150",
                                  evidence_ref="EV-TRANS-1")
        svc.report_public_service("u_ps", "2030", "PS1", "80",
                                  metric_text="共享测试平台机时",
                                  evidence_ref="EV-PS-1",
                                  project_code="P1")
        report = svc.management_report("2030")
        # 成果转化增加值 150 计入战新毛额
        self.assertEqual(
            report["metrics"]["strategic_va_ratio"][
                "gross_value_added"], "3150.00")
        self.assertEqual(
            report["metrics"]["public_service_total"], "80.00")


if __name__ == "__main__":
    unittest.main()
