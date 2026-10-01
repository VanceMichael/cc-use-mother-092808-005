"""价值贡献治理测试的公共夹具。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.valuegov import ValueGovService

ADMIN = "system-admin"
REVIEWER = "reviewer-1"
PLANNER = "planner-1"
UNIT_USER = "unit-user"


def make_clock(year=2026, month=1, day=1):
    current = [datetime(year, month, day, tzinfo=timezone.utc)]

    def clock():
        return current[0]

    clock.current = current
    return clock


def make_service(db_path=":memory:", clock=None, evidence_sla_hours=24 * 7):
    """构造带标准主数据的服务：集团、产业板块、研究院、公共服务单位。"""
    service = ValueGovService(
        db_path, clock=clock or make_clock(), evidence_sla_hours=evidence_sla_hours
    )
    service.register_principal(ADMIN, REVIEWER, ["REVIEWER"])
    service.register_principal(ADMIN, PLANNER, ["PLANNER"])
    service.register_principal(ADMIN, UNIT_USER, ["UNIT_SUBMITTER"])
    service.create_unit(ADMIN, "GRP", "集团总部", "GROUP")
    service.create_unit(ADMIN, "SEG-A", "产业板块A", "SEGMENT", parent_code="GRP")
    service.create_unit(ADMIN, "INS-B", "研究院B", "INSTITUTE", parent_code="GRP")
    service.create_unit(ADMIN, "SVC-C", "公共服务单位C", "SERVICE", parent_code="GRP")
    service.create_category(ADMIN, "SE-NE", "新能源", is_strategic_emerging=True)
    service.create_category(ADMIN, "TRAD", "传统产业")
    service.open_period(PLANNER, "2029")
    service.open_period(PLANNER, "2030")
    return service


def make_se_project(service, code="PRJ-SE", owner="SEG-A", name="战新项目"):
    """创建并经复核批准归入战略性新兴产业分类的项目。"""
    result = service.create_project(UNIT_USER, code, name, owner, category_code="SE-NE")
    service.approve_evidence(REVIEWER, result["classification_evidence_id"])
    return result["project_id"]
