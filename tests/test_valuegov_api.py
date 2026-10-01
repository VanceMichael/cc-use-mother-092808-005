"""HTTP API 冒烟测试。"""

import http.client
import json
import threading
import unittest

from src.valuegov import make_server
from tests.valuegov_fixture import PLANNER, REVIEWER, UNIT_USER, make_service


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = make_service()
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.close()

    def _call(self, method, path, body=None, actor="system-admin"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor"] = actor
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_full_flow_over_http(self):
        status, health = self._call("GET", "/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        # 缺少操作者身份
        status, error = self._call("POST", "/projects", {"code": "API-1"}, actor=None)
        self.assertEqual(status, 401)
        self.assertEqual(error["error"]["code"], "UNAUTHENTICATED")

        # 业务单位越权维护主数据
        status, error = self._call(
            "POST", "/units", {"code": "X-9", "name": "越权单位", "unit_type": "SEGMENT"}, actor=UNIT_USER
        )
        self.assertEqual(status, 403)

        # 创建项目并申请战新归类
        status, project = self._call(
            "POST", "/projects",
            {"code": "API-1", "name": "接口项目", "owner_unit_code": "SEG-A", "category_code": "SE-NE"},
            actor=UNIT_USER,
        )
        self.assertEqual(status, 200)
        evidence_id = project["classification_evidence_id"]

        # 提交人自行批准被拒
        status, error = self._call("POST", f"/evidence/{evidence_id}/approve", {}, actor=UNIT_USER)
        self.assertEqual(status, 403)
        status, _ = self._call("POST", f"/evidence/{evidence_id}/approve", {}, actor=REVIEWER)
        self.assertEqual(status, 200)

        # 入账转化成果与费用
        status, _ = self._call(
            "POST", "/transformations",
            {"project_code": "API-1", "period": "2030", "revenue": 500, "value_added": 200},
            actor=UNIT_USER,
        )
        self.assertEqual(status, 200)
        status, expense = self._call(
            "POST", "/expenses",
            {"project_code": "API-1", "period": "2030", "amount": 40, "kind": "PERSONNEL"},
            actor=UNIT_USER,
        )
        self.assertEqual(status, 200)

        # 设置目标并关账
        status, _ = self._call(
            "POST", "/targets",
            {"metric": "se_value_added_ratio", "period": "2030", "value": 0.5},
            actor=PLANNER,
        )
        self.assertEqual(status, 200)
        status, job = self._call("POST", "/periods/2030/close", {}, actor=PLANNER)
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "DONE")

        # 关账后写入返回 409
        status, error = self._call(
            "POST", "/expenses",
            {"project_code": "API-1", "period": "2030", "amount": 1, "kind": "OTHER"},
            actor=UNIT_USER,
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "PERIOD_CLOSED")

        # 管理层报告与走势
        status, report = self._call("GET", "/periods/2030/report")
        self.assertEqual(status, 200)
        self.assertTrue(report["frozen"])
        metric = {m["metric"]: m for m in report["metrics"]}["se_value_added_ratio"]
        self.assertEqual(metric["actual"], 1.0)
        self.assertEqual(metric["target"], 0.5)

        status, trend = self._call("GET", "/reports/ratios?periods=2029,2030")
        self.assertEqual(status, 200)
        self.assertEqual(len(trend), 2)

        status, error = self._call("GET", "/no-such-path")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
