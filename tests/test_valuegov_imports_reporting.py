"""批量导入的重送识别与复核处置，以及管理层报告内容。"""

import unittest

from src.valuegov import DomainError, period_report, ratio_trend
from tests.valuegov_fixture import PLANNER, REVIEWER, UNIT_USER, make_se_project, make_service


def expense_item(key, amount, project="PRJ-I", period="2030", **extra):
    payload = {"project_code": project, "period": period, "amount": amount, "kind": "PERSONNEL"}
    payload.update(extra)
    return {"key": key, "type": "EXPENSE", "payload": payload}


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.service.create_project(UNIT_USER, "PRJ-I", "导入项目", "SEG-A", category_code="TRAD")

    def tearDown(self):
        self.service.close()

    def _rd_total(self):
        return period_report(self.service, "2030")["figures"]["rd_expense"]

    def test_resend_is_duplicate_and_conflict_goes_to_review(self):
        item = expense_item("ERP-1", 100)
        first = self.service.import_batch(UNIT_USER, "erp", [item])
        self.assertEqual(first["counts"], {"APPLIED": 1, "DUPLICATE": 0, "CONFLICT": 0})
        self.assertEqual(self._rd_total(), 100.0)

        resend = self.service.import_batch(UNIT_USER, "erp", [item])
        self.assertEqual(resend["counts"]["DUPLICATE"], 1)
        self.assertEqual(self._rd_total(), 100.0, "重送不得重复入账")

        changed = self.service.import_batch(UNIT_USER, "erp", [expense_item("ERP-1", 150)])
        self.assertEqual(changed["counts"]["CONFLICT"], 1)
        reviews = self.service.list_reviews("OPEN")
        self.assertEqual(len(reviews), 1)
        self.assertIn("不一致", reviews[0]["reason"])

        # 维持原样：数据不变
        self.service.resolve_review(REVIEWER, reviews[0]["review_id"], "KEEP_EXISTING")
        self.assertEqual(self._rd_total(), 100.0)

        # 再次重送冲突材料：仍是重复，不生成新的复核任务
        again = self.service.import_batch(UNIT_USER, "erp", [expense_item("ERP-1", 150)])
        self.assertEqual(again["counts"]["DUPLICATE"], 1)
        self.assertEqual(self.service.list_reviews("OPEN"), [])

        # 新的冲突版本：复核后替换为更正版本
        third = self.service.import_batch(UNIT_USER, "erp", [expense_item("ERP-1", 170)])
        self.assertEqual(third["counts"]["CONFLICT"], 1)
        review = self.service.list_reviews("OPEN")[0]
        result = self.service.resolve_review(REVIEWER, review["review_id"], "REPLACE")
        self.assertIsNotNone(result["applied_entity"])
        self.assertEqual(self._rd_total(), 170.0)
        corrections = self.service.list_corrections()
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["status"], "APPROVED")
        self.assertIn("导入复核", corrections[0]["reason"])

    def test_imported_basic_research_claim_still_needs_approval(self):
        item = expense_item("ERP-2", 60, is_basic_research=True, basis="导入声明")
        result = self.service.import_batch(UNIT_USER, "erp", [item])
        self.assertEqual(result["counts"]["APPLIED"], 1)
        report = period_report(self.service, "2030")
        self.assertEqual(report["figures"]["rd_expense_basic"], 0.0, "导入声明不得绕过审批")
        pending = self.service.list_evidence("PENDING")
        self.assertEqual(len(pending), 1)
        self.service.approve_evidence(REVIEWER, pending[0]["evidence_id"])
        self.assertEqual(period_report(self.service, "2030")["figures"]["rd_expense_basic"], 60.0)

    def test_import_into_closed_period_is_routed_to_review(self):
        self.service.request_period_close(PLANNER, "2030")
        result = self.service.import_batch(UNIT_USER, "erp", [expense_item("ERP-3", 10)])
        self.assertEqual(result["counts"]["CONFLICT"], 1)
        review = self.service.list_reviews("OPEN")[0]
        self.assertIn("已关账", review["reason"])
        with self.assertRaises(DomainError, msg="期间仍关账时 REPLACE 重试必须失败"):
            self.service.resolve_review(REVIEWER, review["review_id"], "REPLACE")
        self.service.resolve_review(REVIEWER, review["review_id"], "KEEP_EXISTING")
        self.assertEqual(self.service.list_reviews("OPEN"), [])


class ManagementReportTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        make_se_project(self.service, "PRJ-M1", "SEG-A")
        self.service.create_project(UNIT_USER, "PRJ-M2", "传统项目", "INS-B", category_code="TRAD")
        self.service.record_transformation(UNIT_USER, "PRJ-M1", "2030", 2000, 800)
        self.service.record_transformation(UNIT_USER, "PRJ-M2", "2030", 900, 400)
        self.service.record_expense(UNIT_USER, "PRJ-M1", "2030", 150, "PERSONNEL")
        basic = self.service.record_expense(UNIT_USER, "PRJ-M1", "2030", 50, "PERSONNEL")["expense_id"]
        claim = self.service.claim_basic_research(UNIT_USER, basic, "基础研究")["evidence_id"]
        self.service.approve_evidence(REVIEWER, claim)
        self.service.record_expense(UNIT_USER, "PRJ-M2", "2030", 100, "MATERIAL")
        self.service.record_public_service(UNIT_USER, "SVC-C", "2030", "应急保障", 80, "全社会")
        self.service.record_internal_transaction(
            UNIT_USER, "SEG-A", "INS-B", "2030", 100, 50, project_code="PRJ-M1"
        )
        self.service.set_target(PLANNER, "se_value_added_ratio", "2030", 0.7)
        self.service.set_target(PLANNER, "basic_research_ratio", "2030", 0.2)
        self.service.run_elimination(REVIEWER, "2030")
        self.service.request_period_close(PLANNER, "2030")
        # 关账后发现历史错误：通过更正版本处理
        tid = self.service.store.q1(
            "SELECT transformation_id FROM transformations t JOIN projects p ON p.project_id=t.project_id "
            "WHERE p.code='PRJ-M2'"
        )["transformation_id"]
        correction = self.service.create_correction(
            UNIT_USER, "transformation", tid, {"value_added": 500}, "关账后复核发现口径错误"
        )["correction_id"]
        self.service.approve_correction(REVIEWER, correction)

    def tearDown(self):
        self.service.close()

    def test_report_shows_frozen_and_restated_truth(self):
        report = period_report(self.service, "2030")
        self.assertTrue(report["frozen"])
        metrics = {m["metric"]: m for m in report["metrics"]}

        se = metrics["se_value_added_ratio"]
        self.assertAlmostEqual(se["actual"], 750 / 1150, places=6)
        self.assertEqual(se["target"], 0.7)
        self.assertAlmostEqual(se["gap"], 750 / 1150 - 0.7, places=6)
        self.assertAlmostEqual(se["gap_points"], (750 / 1150 - 0.7) * 100, places=4)

        basic = metrics["basic_research_ratio"]
        self.assertAlmostEqual(basic["actual"], 50 / 300, places=6)
        self.assertAlmostEqual(basic["gap"], 50 / 300 - 0.2, places=6)

        self.assertEqual(report["corrections_after_close"], 1)
        restated = report["restated"]
        self.assertIsNotNone(restated, "关账后更正必须形成重述视图")
        self.assertAlmostEqual(restated["metrics"]["se_value_added_ratio"], 750 / 1250, places=6)

    def test_report_contains_drilldown_elimination_and_responsibility(self):
        report = period_report(self.service, "2030")
        codes = {p["code"] for p in report["projects"]}
        self.assertEqual(codes, {"PRJ-M1", "PRJ-M2"})
        m1 = next(p for p in report["projects"] if p["code"] == "PRJ-M1")
        self.assertTrue(m1["is_strategic_emerging"])
        self.assertEqual(m1["basic_research_expense"], 50.0)
        self.assertEqual(m1["units"][0]["unit_code"], "SEG-A", "无协作规则时归属业主单位")

        runs = report["eliminations"]["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["created_by"], REVIEWER)
        self.assertEqual(runs[0]["entries"][0]["value_added"], 50.0)

        approvals = {(a["kind"], a["subject_type"]): a for a in report["approvals"]}
        self.assertIn(("evidence", "SE_CLASSIFICATION"), approvals)
        self.assertIn(("evidence", "BASIC_RESEARCH_CLAIM"), approvals)
        self.assertIn(("correction", "transformation"), approvals)
        for approval in approvals.values():
            self.assertEqual(approval["decided_by"], REVIEWER, "审批责任必须可追溯")
            self.assertNotEqual(approval["decided_by"], approval["submitted_by"])

    def test_ratio_trend_compares_periods(self):
        self.service.record_expense(UNIT_USER, "PRJ-M2", "2029", 80, "MATERIAL")
        trend = ratio_trend(self.service, ["2029", "2030"])
        self.assertEqual([t["period"] for t in trend], ["2029", "2030"])
        self.assertEqual(trend[0]["status"], "OPEN")
        self.assertEqual(trend[1]["status"], "CLOSED")
        self.assertEqual(trend[0]["metrics"]["basic_research_ratio"]["actual"], 0.0)
        self.assertAlmostEqual(trend[1]["metrics"]["basic_research_ratio"]["actual"], 50 / 300, places=6)
        self.assertEqual(trend[1]["metrics"]["basic_research_ratio"]["target"], 0.2)

        report = period_report(self.service, "2030")
        metric = {m["metric"]: m for m in report["metrics"]}["basic_research_ratio"]
        self.assertEqual(metric["previous"], 0.0, "应给出上一报告期的同口径数值")
        self.assertAlmostEqual(metric["delta"], 50 / 300, places=6)


if __name__ == "__main__":
    unittest.main()
