"""跨单位协作分配、项目拆分、重组与内部交易抵销。"""

import unittest

from src.valuegov import ConflictError, ValidationError, period_report
from tests.valuegov_fixture import ADMIN, REVIEWER, UNIT_USER, make_se_project, make_service


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.service.create_project(UNIT_USER, "PRJ-J", "联合研发项目", "SEG-A", category_code="TRAD")

    def tearDown(self):
        self.service.close()

    def test_allocation_weights_must_sum_to_one(self):
        with self.assertRaises(ValidationError):
            self.service.set_allocation(
                UNIT_USER, "PRJ-J", "2030",
                [{"unit_code": "SEG-A", "weight": 0.5}, {"unit_code": "INS-B", "weight": 0.3}],
                "联合研发协议",
            )
        with self.assertRaises(ValidationError):
            self.service.set_allocation(UNIT_USER, "PRJ-J", "2030", [], "联合研发协议")

    def test_joint_project_is_counted_once_and_attributed_by_rules(self):
        self.service.set_allocation(
            UNIT_USER, "PRJ-J", "2030",
            [
                {"unit_code": "SEG-A", "weight": 0.5},
                {"unit_code": "INS-B", "weight": 0.3},
                {"unit_code": "SVC-C", "weight": 0.2},
            ],
            "联合研发协议：按工作量份额",
        )
        self.service.record_transformation(UNIT_USER, "PRJ-J", "2030", 1000, 600)
        self.service.record_expense(UNIT_USER, "PRJ-J", "2030", 300, "PERSONNEL")

        report = period_report(self.service, "2030")
        # 集团层面只计一次，三方各自认领不会造成重复
        self.assertEqual(report["figures"]["transformation_value_added"], 600.0)
        self.assertEqual(report["figures"]["rd_expense"], 300.0)

        project = next(p for p in report["projects"] if p["code"] == "PRJ-J")
        self.assertEqual(project["value_added"], 600.0)
        shares = {u["unit_code"]: u for u in project["units"]}
        self.assertEqual(shares["SEG-A"]["value_added"], 300.0)
        self.assertEqual(shares["INS-B"]["value_added"], 180.0)
        self.assertEqual(shares["SVC-C"]["value_added"], 120.0)
        self.assertAlmostEqual(sum(u["value_added"] for u in project["units"]), 600.0)
        self.assertTrue(all(u["basis"] for u in project["units"]), "归属必须携带可审计依据")

    def test_allocation_can_be_replaced_with_audit_trail(self):
        self.service.set_allocation(
            UNIT_USER, "PRJ-J", "2030",
            [{"unit_code": "SEG-A", "weight": 0.6}, {"unit_code": "INS-B", "weight": 0.4}],
            "初版协议",
        )
        self.service.set_allocation(
            UNIT_USER, "PRJ-J", "2030",
            [{"unit_code": "SEG-A", "weight": 0.7}, {"unit_code": "INS-B", "weight": 0.3}],
            "修订协议",
        )
        active = self.service.store.qa(
            "SELECT * FROM allocation_rules WHERE effective_period='2030' AND superseded_by IS NULL"
        )
        self.assertEqual(len(active), 2)
        history = self.service.store.qa("SELECT * FROM allocation_rules WHERE effective_period='2030'")
        self.assertEqual(len(history), 4, "被取代的规则必须留痕")


class SplitAndReorgTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def tearDown(self):
        self.service.close()

    def test_split_project_blocks_parent_and_inherits_collaboration(self):
        self.service.create_project(UNIT_USER, "PRJ-S", "待拆分项目", "SEG-A", category_code="TRAD")
        self.service.set_allocation(
            UNIT_USER, "PRJ-S", "2030",
            [{"unit_code": "SEG-A", "weight": 0.6}, {"unit_code": "INS-B", "weight": 0.4}],
            "联合研发协议",
        )
        result = self.service.split_project(
            ADMIN, "PRJ-S", "2030",
            [
                {"code": "PRJ-S1", "name": "子项目一", "owner_unit_code": "SEG-A", "weight": 0.7},
                {"code": "PRJ-S2", "name": "子项目二", "owner_unit_code": "INS-B", "weight": 0.3},
            ],
            "立项调整",
        )
        self.assertEqual(len(result["children"]), 2)
        with self.assertRaises(ConflictError, msg="拆分后父项目不得再入账"):
            self.service.record_transformation(UNIT_USER, "PRJ-S", "2030", 100, 50)
        self.service.create_project(UNIT_USER, "PRJ-T", "单子项目", "SEG-A", category_code="TRAD")
        with self.assertRaises(ValidationError):
            self.service.split_project(
                ADMIN, "PRJ-T", "2030",
                [{"code": "PRJ-T1", "name": "一", "owner_unit_code": "SEG-A", "weight": 1.0}],
                "只有一个子项目",
            )

        self.service.record_transformation(UNIT_USER, "PRJ-S1", "2030", 700, 350)
        report = period_report(self.service, "2030")
        child = next(p for p in report["projects"] if p["code"] == "PRJ-S1")
        shares = {u["unit_code"]: u for u in child["units"]}
        self.assertEqual(shares["SEG-A"]["weight"], 0.6)
        self.assertEqual(shares["INS-B"]["value_added"], 140.0)
        self.assertAlmostEqual(sum(u["value_added"] for u in child["units"]), 350.0)

    def test_reorg_moves_attribution_and_retires_unit(self):
        self.service.record_public_service(UNIT_USER, "SVC-C", "2029", "检验检测", 100, "集团内各单位")
        self.service.register_reorg(ADMIN, "SVC-C", "SEG-A", "2030", "专业化整合")

        with self.assertRaises(ConflictError, msg="退出单位不得在生效期后入账"):
            self.service.record_public_service(UNIT_USER, "SVC-C", "2030", "检验检测", 50, "集团内各单位")

        scope_2029 = self.service._org_scope("2029")
        scope_2030 = self.service._org_scope("2030")
        svc_id = self.service.store.q1("SELECT unit_id FROM org_units WHERE code='SVC-C'")["unit_id"]
        self.assertIn(svc_id, scope_2029)
        self.assertNotIn(svc_id, scope_2030)

        report_2029 = period_report(self.service, "2029")
        self.assertEqual(report_2029["figures"]["public_service_value"], 100.0)
        # 重组事件全程留痕
        events = self.service.store.qa("SELECT * FROM reorg_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "专业化整合")


class EliminationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        make_se_project(self.service, "PRJ-SE", "SEG-A")
        self.service.create_project(UNIT_USER, "PRJ-N", "传统项目", "INS-B", category_code="TRAD")

    def tearDown(self):
        self.service.close()

    def test_elimination_leaves_only_net_effect(self):
        self.service.record_transformation(UNIT_USER, "PRJ-SE", "2030", 2000, 1000)
        self.service.record_transformation(UNIT_USER, "PRJ-N", "2030", 1500, 1000)
        self.service.record_internal_transaction(
            UNIT_USER, "SEG-A", "INS-B", "2030", 400, 300, project_code="PRJ-SE", description="内部协作结算"
        )
        before = period_report(self.service, "2030")
        metric = {m["metric"]: m for m in before["metrics"]}["se_value_added_ratio"]
        self.assertEqual(metric["actual"], 0.5)

        run = self.service.run_elimination(REVIEWER, "2030")
        self.assertEqual(run["eliminated"], 1)

        after = period_report(self.service, "2030")
        metric = {m["metric"]: m for m in after["metrics"]}["se_value_added_ratio"]
        self.assertAlmostEqual(metric["actual"], 700 / 1700, places=6)
        revenue = {m["metric"]: m for m in after["metrics"]}["revenue_total"]
        self.assertEqual(revenue["actual"], 3100.0, "收入只保留抵销后的净影响")

        elimination = after["eliminations"]
        self.assertEqual(len(elimination["runs"]), 1)
        entry = elimination["runs"][0]["entries"][0]
        self.assertEqual(entry["amount"], 400.0)
        self.assertEqual(entry["value_added"], 300.0)
        self.assertEqual(entry["seller_unit_code"], "SEG-A")
        self.assertEqual(elimination["open_transactions"], [])
        # 重复执行不会二次抵销
        self.assertEqual(self.service.run_elimination(REVIEWER, "2030")["eliminated"], 0)

    def test_uneliminated_transactions_are_visible_as_pending(self):
        self.service.record_transformation(UNIT_USER, "PRJ-SE", "2030", 100, 50)
        self.service.record_internal_transaction(UNIT_USER, "SEG-A", "INS-B", "2030", 30, 10)
        report = period_report(self.service, "2030")
        self.assertEqual(len(report["eliminations"]["open_transactions"]), 1)
        self.assertEqual(report["eliminations"]["runs"], [])


if __name__ == "__main__":
    unittest.main()
