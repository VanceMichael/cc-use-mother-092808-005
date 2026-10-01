"""价值贡献治理后端的 HTTP JSON API（仅使用标准库）。

所有变更类请求需要 `X-Actor` 头标识操作者；错误以
`{"error": {"code", "message"}}` 形式返回，状态码与领域错误对应。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .reporting import period_report, ratio_trend

_ROUTES = []


def _route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorate(func):
        _ROUTES.append((method, regex, func))
        return func

    return decorate


# ---------------------------------------------------------------- 只读端点

_route("GET", "/health")(lambda svc, actor, body: {"status": "ok"})
_route("GET", "/units")(lambda svc, actor, body: svc.list_units())
_route("GET", "/categories")(lambda svc, actor, body: svc.list_categories())
_route("GET", "/periods")(lambda svc, actor, body: svc.list_periods())
_route("GET", "/periods/{period}")(lambda svc, actor, body, period: svc.get_period(period))
_route("GET", "/periods/{period}/report")(lambda svc, actor, body, period: period_report(svc, period))
_route("GET", "/projects/{code}")(lambda svc, actor, body, code: svc.get_project(code))
_route("GET", "/evidence")(lambda svc, actor, body: svc.list_evidence(_query(body, "status")))
_route("GET", "/corrections")(lambda svc, actor, body: svc.list_corrections(_query(body, "status")))
_route("GET", "/targets")(lambda svc, actor, body: svc.list_targets(_query(body, "period")))
_route("GET", "/imports/{batch_id}")(lambda svc, actor, body, batch_id: svc.get_import_batch(batch_id))
_route("GET", "/reviews")(lambda svc, actor, body: svc.list_reviews(_query(body, "status")))
_route("GET", "/reminders")(lambda svc, actor, body: svc.list_reminders())
_route("GET", "/jobs")(lambda svc, actor, body: svc.list_jobs())
_route("GET", "/jobs/{job_id}")(lambda svc, actor, body, job_id: svc.get_job(job_id))
_route("GET", "/audit")(lambda svc, actor, body: svc.audit_trail(_query(body, "entity_type"), _query(body, "entity_id")))


def _ratios(svc, actor, body):
    periods = _query(body, "periods")
    if not periods:
        raise DomainError("缺少 periods 查询参数")
    return ratio_trend(svc, [p for p in periods.split(",") if p])


_route("GET", "/reports/ratios")(_ratios)


def _query(body: dict, name: str):
    values = body.get("_query", {}).get(name)
    return values[0] if values else None


# ---------------------------------------------------------------- 变更端点

_route("POST", "/principals")(lambda svc, actor, body: svc.register_principal(actor, body["name"], body["roles"]))
_route("POST", "/units")(
    lambda svc, actor, body: svc.create_unit(
        actor, body["code"], body["name"], body["unit_type"], body.get("parent_code")
    )
)
_route("POST", "/reorgs")(
    lambda svc, actor, body: svc.register_reorg(
        actor, body["from_unit_code"], body["to_unit_code"], body["effective_period"], body["reason"]
    )
)
_route("POST", "/categories")(
    lambda svc, actor, body: svc.create_category(
        actor, body["code"], body["name"], body.get("is_strategic_emerging", False), body.get("parent_code")
    )
)
_route("POST", "/formula-versions")(lambda svc, actor, body: svc.publish_formula(actor, body["definition"]))
_route("POST", "/periods/open")(lambda svc, actor, body: svc.open_period(actor, body["period"]))
_route("POST", "/periods/{period}/close")(lambda svc, actor, body, period: svc.request_period_close(actor, period))
_route("POST", "/targets")(
    lambda svc, actor, body: svc.set_target(actor, body["metric"], body["period"], body["value"])
)
_route("POST", "/projects")(
    lambda svc, actor, body: svc.create_project(
        actor, body["code"], body["name"], body["owner_unit_code"], body.get("category_code")
    )
)
_route("POST", "/projects/{code}/classify")(
    lambda svc, actor, body, code: svc.request_se_classification(
        actor, code, body["category_code"], body["justification"]
    )
)
_route("POST", "/projects/{code}/allocation")(
    lambda svc, actor, body, code: svc.set_allocation(
        actor, code, body["period"], body["shares"], body["basis"]
    )
)
_route("POST", "/projects/{code}/split")(
    lambda svc, actor, body, code: svc.split_project(
        actor, code, body["period"], body["children"], body["reason"]
    )
)
_route("POST", "/investment-batches")(
    lambda svc, actor, body: svc.record_investment(
        actor, body["project_code"], body["period"], body["amount"], body["funding_source"], body.get("note", "")
    )
)
_route("POST", "/expenses")(
    lambda svc, actor, body: svc.record_expense(
        actor, body["project_code"], body["period"], body["amount"], body["kind"]
    )
)
_route("POST", "/expenses/{expense_id}/basic-research-claim")(
    lambda svc, actor, body, expense_id: svc.claim_basic_research(actor, expense_id, body["basis"])
)
_route("POST", "/transformations")(
    lambda svc, actor, body: svc.record_transformation(
        actor, body["project_code"], body["period"], body.get("revenue", 0),
        body.get("value_added", 0), body.get("description", ""),
    )
)
_route("POST", "/public-service")(
    lambda svc, actor, body: svc.record_public_service(
        actor, body["unit_code"], body["period"], body["service_type"], body["value"],
        body["beneficiary_scope"], body.get("description", ""),
    )
)
_route("POST", "/internal-transactions")(
    lambda svc, actor, body: svc.record_internal_transaction(
        actor, body["seller_unit_code"], body["buyer_unit_code"], body["period"], body["amount"],
        body.get("value_added", 0), body.get("project_code"), body.get("description", ""),
    )
)
_route("POST", "/periods/{period}/elimination-runs")(
    lambda svc, actor, body, period: svc.run_elimination(actor, period)
)
_route("POST", "/evidence/{evidence_id}/approve")(
    lambda svc, actor, body, evidence_id: svc.approve_evidence(actor, evidence_id, body.get("note"))
)
_route("POST", "/evidence/{evidence_id}/reject")(
    lambda svc, actor, body, evidence_id: svc.reject_evidence(actor, evidence_id, body["note"])
)
_route("POST", "/corrections")(
    lambda svc, actor, body: svc.create_correction(
        actor, body["entity_type"], body["entity_id"], body["changes"], body["reason"]
    )
)
_route("POST", "/corrections/{correction_id}/approve")(
    lambda svc, actor, body, correction_id: svc.approve_correction(actor, correction_id)
)
_route("POST", "/corrections/{correction_id}/reject")(
    lambda svc, actor, body, correction_id: svc.reject_correction(actor, correction_id, body["note"])
)
_route("POST", "/imports")(
    lambda svc, actor, body: svc.import_batch(actor, body["source"], body["items"])
)
_route("POST", "/reviews/{review_id}/resolve")(
    lambda svc, actor, body, review_id: svc.resolve_review(actor, review_id, body["action"], body.get("note"))
)
_route("POST", "/evidence-requests")(
    lambda svc, actor, body: svc.create_evidence_request(
        actor, body["subject_type"], body["subject_id"], body["unit_code"],
        body["description"], body["due_at"],
    )
)
_route("POST", "/jobs/sweep")(lambda svc, actor, body: svc.run_reminder_sweep())
_route("POST", "/jobs/recover")(lambda svc, actor, body: svc.recover() or {"recovered": True})


class _Handler(BaseHTTPRequestHandler):
    service = None
    server_version = "ValueGov/0.1"

    def log_message(self, format, *args):  # 保持测试输出安静
        pass

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        for route_method, regex, func in _ROUTES:
            if route_method != method:
                continue
            match = regex.match(parsed.path)
            if not match:
                continue
            try:
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        body = json.loads(raw.decode("utf-8"))
                        if not isinstance(body, dict):
                            raise DomainError("请求体必须是 JSON 对象")
                body["_query"] = parse_qs(parsed.query)
                actor = self.headers.get("X-Actor")
                if method == "POST" and not actor:
                    self._send(401, {"error": {"code": "UNAUTHENTICATED", "message": "缺少 X-Actor 头"}})
                    return
                result = func(self.service, actor, body, **match.groupdict())
                self._send(200, result)
            except DomainError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except (KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"error": {"code": "BAD_REQUEST", "message": f"请求无效: {exc}"}})
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})
            return
        self._send(404, {"error": {"code": "NOT_FOUND", "message": f"未知路径: {parsed.path}"}})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def make_server(service, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("ValueGovHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)
