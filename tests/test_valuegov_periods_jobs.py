"""关账快照冻结与可恢复作业：重启后未完成的关账与催补不丢。"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from src.valuegov import PeriodClosedError, ValueGovService, period_report
from tests.valuegov_fixture import (
    ADMIN,
    PLANNER,
    REVIEWER,
    UNIT_USER,
    make_clock,
    make_service,
)


class PeriodCloseTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.service.create_project(UNIT_USER, "PRJ-A", "战新项目", "SEG-A", category_code="TRAD")
        self.service.record_transformation(UNIT_USER, "PRJ-A", "2030", 1000, 400)
        self.service.record_expense(UNIT_USER, "PRJ-A", "2030", 100, "PERSONNEL")

    def tearDown(self):
        self.service.close()

    def test_close_freezes_formula_scope_and_targets(self):
        self.service.set_target(PLANNER, "se_value_added_ratio", "2030", 0.3)
        job = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(job["status"], "DONE")

        with self.assertRaises(PeriodClosedError):
            self.service.record_transformation(UNIT_USER, "PRJ-A", "2030", 1, 1)
        with self.assertRaises(PeriodClosedError):
            self.service.record_expense(UNIT_USER, "PRJ-A", "2030", 1, "PERSONNEL")

        # 关账后发布新公式、新设单位，不影响已冻结快照
        self.service.publish_formula(
            ADMIN, {"metrics": {"custom_metric": {"kind": "sum", "source": "investment"}}}
        )
        self.service.create_unit(ADMIN, "SEG-D", "新设板块", "SEGMENT")

        report = period_report(self.service, "2030")
        self.assertTrue(report["frozen"])
        self.assertEqual(report["formula_version"], 1, "快照必须使用关账时的公式版本")
        names = {m["metric"] for m in report["metrics"]}
        self.assertIn("se_value_added_ratio", names)
        self.assertNotIn("custom_metric", names)
        metric = {m["metric"]: m for m in report["metrics"]}["se_value_added_ratio"]
        self.assertEqual(metric["target"], 0.3, "目标随快照冻结")
        self.assertEqual(metric["actual"], 0.0, "PRJ-A 为传统产业，战新分子为 0")

        snapshot = self.service.store.q1("SELECT * FROM snapshots WHERE period='2030'")
        self.assertIsNotNone(snapshot)

    def test_close_is_blocked_by_pending_work_and_is_idempotent(self):
        expense_id = self.service.record_expense(UNIT_USER, "PRJ-A", "2030", 20, "OTHER")["expense_id"]
        evidence_id = self.service.claim_basic_research(UNIT_USER, expense_id, "基础研究")["evidence_id"]

        job = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("待审批证据", job["error"])
        self.assertEqual(self.service.get_period("2030")["status"], "OPEN", "关账失败必须回滚为开放")

        self.service.approve_evidence(REVIEWER, evidence_id)
        self.service.record_internal_transaction(UNIT_USER, "SEG-A", "INS-B", "2030", 50, 10)
        job = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("未抵销", job["error"])

        self.service.run_elimination(REVIEWER, "2030")
        job = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(job["status"], "DONE")

        again = self.service.request_period_close(PLANNER, "2030")
        self.assertEqual(again["status"], "DONE")
        self.assertEqual(again["result"]["snapshot_id"], job["result"]["snapshot_id"], "重复关账必须幂等")


class RecoveryTest(unittest.TestCase):
    def _build(self, path, clock):
        service = ValueGovService(path, clock=clock, evidence_sla_hours=24)
        service.register_principal(ADMIN, REVIEWER, ["REVIEWER"])
        service.register_principal(ADMIN, PLANNER, ["PLANNER"])
        service.register_principal(ADMIN, UNIT_USER, ["UNIT_SUBMITTER"])
        service.create_unit(ADMIN, "GRP", "集团总部", "GROUP")
        service.create_unit(ADMIN, "SEG-A", "产业板块A", "SEGMENT", parent_code="GRP")
        service.create_unit(ADMIN, "INS-B", "研究院B", "INSTITUTE", parent_code="GRP")
        service.create_category(ADMIN, "TRAD", "传统产业")
        service.open_period(PLANNER, "2030")
        service.create_project(UNIT_USER, "PRJ-R", "恢复项目", "SEG-A", category_code="TRAD")
        service.record_transformation(UNIT_USER, "PRJ-R", "2030", 100, 40)
        return service

    def test_unfinished_close_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gov.db")
            clock = make_clock()
            first = self._build(path, clock)
            job_id = first.enqueue_job("PERIOD_CLOSE", {"period": "2030", "actor": PLANNER})
            # 模拟进程崩溃：作业停在 RUNNING，快照尚未生成
            with first.store.tx():
                first.store.run("UPDATE jobs SET status='RUNNING' WHERE job_id=?", (job_id,))
            first.close()

            second = ValueGovService(path, clock=clock, evidence_sla_hours=24)
            try:
                job = second.get_job(job_id)
                self.assertEqual(job["status"], "DONE", "重启恢复必须重新执行未完成的关账")
                self.assertEqual(second.get_period("2030")["status"], "CLOSED")
                report = period_report(second, "2030")
                self.assertTrue(report["frozen"])
            finally:
                second.close()

    def test_reminders_survive_restart_and_stay_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gov.db")
            clock = make_clock(2026, 1, 1)
            first = self._build(path, clock)
            expense_id = first.record_expense(UNIT_USER, "PRJ-R", "2030", 10, "OTHER")["expense_id"]
            first.claim_basic_research(UNIT_USER, expense_id, "基础研究")
            first.create_evidence_request(
                PLANNER, "BASIC_RESEARCH_CLAIM", expense_id, "SEG-A", "请补充立项批复", "2026-01-05T00:00:00+00:00"
            )
            # 时间推进到 SLA 与催补期限之后
            clock.current[0] = datetime(2026, 1, 10, tzinfo=timezone.utc)
            self.assertEqual(first.run_reminder_sweep()["created"], 2)
            first.close()

            second = ValueGovService(path, clock=clock, evidence_sla_hours=24)
            try:
                # 重启触发的恢复扫描不得重复生成提醒
                reminders = second.list_reminders()
                self.assertEqual(len(reminders), 2)
                kinds = {r["kind"] for r in reminders}
                self.assertEqual(kinds, {"EVIDENCE_OVERDUE", "REQUEST_OVERDUE"})
                self.assertEqual(second.run_reminder_sweep()["created"], 0)
            finally:
                second.close()

    def test_evidence_request_is_fulfilled_when_evidence_approved(self):
        service = make_service()
        try:
            service.create_project(UNIT_USER, "PRJ-E", "催补项目", "INS-B", category_code="TRAD")
            expense_id = service.record_expense(UNIT_USER, "PRJ-E", "2030", 10, "OTHER")["expense_id"]
            evidence_id = service.claim_basic_research(UNIT_USER, expense_id, "基础研究")["evidence_id"]
            request_id = service.create_evidence_request(
                PLANNER, "BASIC_RESEARCH_CLAIM", expense_id, "INS-B", "请补充说明", "2026-02-01T00:00:00+00:00"
            )["request_id"]
            service.approve_evidence(REVIEWER, evidence_id)
            row = service.store.q1("SELECT status FROM evidence_requests WHERE request_id=?", (request_id,))
            self.assertEqual(row["status"], "FULFILLED")
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
