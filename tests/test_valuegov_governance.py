"""治理权责：特殊归类审批、基础研究属性、目标期间约束、更正版本。"""

import unittest

from src.valuegov import (
    ConflictError,
    ForbiddenError,
    PeriodClosedError,
    period_report,
)
from tests.valuegov_fixture import (
    ADMIN,
    PLANNER,
    REVIEWER,
    UNIT_USER,
    make_se_project,
    make_service,
)


class SpecialClassificationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def tearDown(self):
        self.service.close()

    def test_se_classification_requires_review_and_never_self_approval(self):
        result = self.service.create_project(UNIT_USER, "PRJ-1", "联合研发项目", "SEG-A", category_code="SE-NE")
        project = self.service.get_project("PRJ-1")
        self.assertIsNone(project["category_id"], "未经批准战新归类不得生效")
        self.assertEqual(project["pending_category_code"], "SE-NE")

        evidence_id = result["classification_evidence_id"]
        with self.assertRaises(ForbiddenError, msg="提交人不得自行批准"):
            self.service.approve_evidence(UNIT_USER, evidence_id)
        with self.assertRaises(ForbiddenError, msg="规划人员没有复核角色"):
            self.service.approve_evidence(PLANNER, evidence_id)

        self.service.approve_evidence(REVIEWER, evidence_id)
        self.assertEqual(self.service.get_project("PRJ-1")["category_code"], "SE-NE")

    def test_rejected_classification_clears_pending_state(self):
        result = self.service.create_project(UNIT_USER, "PRJ-2", "项目二", "INS-B", category_code="SE-NE")
        self.service.reject_evidence(REVIEWER, result["classification_evidence_id"], "依据不足")
        project = self.service.get_project("PRJ-2")
        self.assertIsNone(project["pending_category_id"])
        # 驳回后可以重新申请
        again = self.service.request_se_classification(UNIT_USER, "PRJ-2", "SE-NE", "补充材料后重新申报")
        self.service.approve_evidence(REVIEWER, again["evidence_id"])
        self.assertEqual(self.service.get_project("PRJ-2")["category_code"], "SE-NE")

    def test_basic_research_claim_only_counts_after_approval(self):
        self.service.create_project(UNIT_USER, "PRJ-3", "基础研究项目", "INS-B", category_code="TRAD")
        expense_id = self.service.record_expense(UNIT_USER, "PRJ-3", "2030", 100, "PERSONNEL")["expense_id"]
        evidence_id = self.service.claim_basic_research(UNIT_USER, expense_id, "面向前沿基础研究")["evidence_id"]

        report = period_report(self.service, "2030")
        self.assertEqual(report["figures"]["rd_expense_basic"], 0.0, "未批准的申报不得计入")

        with self.assertRaises(ForbiddenError):
            self.service.approve_evidence(UNIT_USER, evidence_id)
        self.service.approve_evidence(REVIEWER, evidence_id)

        report = period_report(self.service, "2030")
        self.assertEqual(report["figures"]["rd_expense_basic"], 100.0)
        metric = {m["metric"]: m for m in report["metrics"]}["basic_research_ratio"]
        self.assertEqual(metric["actual"], 1.0)


class TargetAndCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def tearDown(self):
        self.service.close()

    def test_targets_only_affect_open_periods(self):
        self.service.set_target(PLANNER, "se_value_added_ratio", "2030", 0.35)
        job = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(job["status"], "DONE")
        with self.assertRaises(PeriodClosedError):
            self.service.set_target(PLANNER, "se_value_added_ratio", "2030", 0.4)
        # 未来期间（尚未开立）仍可设置
        self.service.set_target(PLANNER, "se_value_added_ratio", "2031", 0.4)
        # 目标修改保留版本链
        self.service.set_target(PLANNER, "se_value_added_ratio", "2029", 0.3)
        self.service.set_target(PLANNER, "se_value_added_ratio", "2029", 0.32)
        targets = [t for t in self.service.list_targets("2029") if t["metric"] == "se_value_added_ratio"]
        self.assertEqual(len(targets), 2)
        active = [t for t in targets if t["superseded_by"] is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["target_value"], 0.32)

    def test_correction_versions_replace_figures_and_preserve_history(self):
        self.service.create_project(UNIT_USER, "PRJ-4", "转化项目", "SEG-A", category_code="TRAD")
        tid = self.service.record_transformation(UNIT_USER, "PRJ-4", "2030", 500, 200)["transformation_id"]
        correction_id = self.service.create_correction(
            UNIT_USER, "transformation", tid, {"value_added": 260}, "口径调整"
        )["correction_id"]
        with self.assertRaises(ForbiddenError, msg="更正同样不得自行批准"):
            self.service.approve_correction(UNIT_USER, correction_id)
        result = self.service.approve_correction(REVIEWER, correction_id)

        report = period_report(self.service, "2030")
        self.assertEqual(report["figures"]["transformation_value_added"], 260.0)
        rows = self.service.store.qa(
            "SELECT * FROM transformations WHERE project_id=(SELECT project_id FROM projects WHERE code='PRJ-4') "
            "ORDER BY version"
        )
        self.assertEqual([r["version"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["superseded_by"], result["new_entity_id"])
        self.assertEqual(rows[1]["supersedes_id"], tid)

    def test_void_correction_removes_record_from_figures(self):
        self.service.create_project(UNIT_USER, "PRJ-5", "费用项目", "SEG-A", category_code="TRAD")
        expense_id = self.service.record_expense(UNIT_USER, "PRJ-5", "2030", 80, "MATERIAL")["expense_id"]
        correction_id = self.service.create_correction(
            UNIT_USER, "rd_expense", expense_id, {"void": True}, "重复录入，作废"
        )["correction_id"]
        self.service.approve_correction(REVIEWER, correction_id)
        report = period_report(self.service, "2030")
        self.assertEqual(report["figures"]["rd_expense"], 0.0)

    def test_eliminated_transaction_cannot_be_corrected(self):
        txn_id = self.service.record_internal_transaction(
            UNIT_USER, "SEG-A", "INS-B", "2030", 100, 40
        )["txn_id"]
        self.service.run_elimination(REVIEWER, "2030")
        with self.assertRaises(ConflictError):
            self.service.create_correction(UNIT_USER, "internal_transaction", txn_id, {"amount": 120}, "金额错误")

    def test_unknown_actor_and_missing_role_are_forbidden(self):
        with self.assertRaises(ForbiddenError):
            self.service.create_unit("ghost", "X-1", "影子单位", "SEGMENT")
        with self.assertRaises(ForbiddenError):
            self.service.set_target(UNIT_USER, "se_value_added_ratio", "2030", 0.3)
        with self.assertRaises(ForbiddenError):
            self.service.publish_formula(UNIT_USER, {"metrics": {"x": {"kind": "sum", "source": "investment"}}})


if __name__ == "__main__":
    unittest.main()
