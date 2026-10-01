"""测试共用的最小集团世界：一个战新联合研发项目 + 一个传统项目。"""

from __future__ import annotations

import os

from src.value_governance import GovernanceService


def build_world(path: str = ":memory:") -> GovernanceService:
    svc = GovernanceService(path)
    svc.create_user("office", "集团数据办公室", "DATA_OFFICE")
    svc.create_user("planner", "规划管理人员", "PLANNER")
    svc.create_user("rev", "财务复核人员", "REVIEWER")

    for code, name, kind in (
        ("GROUP", "集团本部", "GROUP"),
        ("SE1", "产业板块", "SECTOR"),
        ("RI1", "研究院", "RESEARCH_INSTITUTE"),
        ("PS1", "公共服务单位", "PUBLIC_SERVICE"),
    ):
        svc.create_organization("office", code, name, kind,
                                None if code == "GROUP" else "GROUP")
    for username, org in (("u_se", "SE1"), ("u_ri", "RI1"),
                          ("u_ps", "PS1")):
        svc.create_user(username, username, "UNIT_USER", org)

    svc.create_industry("office", "I_TRAD", "传统制造", is_strategic=False)
    svc.create_industry("office", "I_STR", "战略性新兴产业", is_strategic=True)

    svc.create_period("office", "2029", "2029年度")
    svc.create_period("office", "2030", "2030年度", "2029")

    svc.create_project("office", "P1", "联合研发项目", "SE1", "I_STR")
    svc.create_project("office", "P2", "传统制造项目", "SE1", "I_TRAD")
    svc.create_project("office", "P1A", "联合项目-产业化子项", "SE1",
                       "I_STR", parent_project="P1")
    svc.create_project("office", "P1B", "联合项目-试验子项", "RI1",
                       "I_STR", parent_project="P1")
    return svc


def seed_prior_period(svc: GovernanceService) -> None:
    """2029 年只有传统项目与少量研发，关账后作为 2030 同比基期。"""
    svc.report_output("u_se", "2029", "P2", "4000", "1800")
    svc.report_rd_expense("u_se", "2029", "P2", "80")
    job = svc.start_close("office", "2029")
    result = svc.execute_close_job(job)
    assert result["status"] == "DONE", result


def seed_2030(svc: GovernanceService) -> dict:
    """2030 年联合项目按 0.5/0.3/0.2 份额申报，毛额 VA 1000、收入 2000。

    返回关键记录 ID，供断言与更正使用。
    """
    svc.set_goal("planner", "2030", "STRATEGIC_VA_RATIO", "0.20")
    svc.set_goal("planner", "2030", "BASIC_RESEARCH_RATIO", "0.15")
    svc.set_goal("planner", "2030", "RD_GROWTH", "0.07")
    svc.define_allocation(
        "office", "2030", "P1", "CONTRACT",
        {"SE1": "0.5", "RI1": "0.3", "PS1": "0.2"},
        note="按联合协议出资与人力构成",
    )
    ids: dict[str, int] = {}
    ids["out_se"] = svc.report_output("u_se", "2030", "P1", "1000", "500")
    ids["out_ri"] = svc.report_output("u_ri", "2030", "P1", "600", "300")
    ids["out_ps"] = svc.report_output("u_ps", "2030", "P1", "400", "200")
    svc.report_rd_expense("u_se", "2030", "P1", "50")
    ids["rd_ri"] = svc.report_rd_expense(
        "u_ri", "2030", "P1", "30", basic_claim=True, evidence_ref="EV-BASIC-1"
    )
    svc.report_rd_expense("u_ps", "2030", "P1", "20")
    svc.report_output("u_se", "2030", "P2", "5000", "2000")

    req = svc.submit_classification(
        "u_ri", "2030", "RD_BASIC", str(ids["rd_ri"]), "true", "EV-BASIC-1"
    )
    svc.review_classification("rev", req, True)

    ids["trade"] = svc.record_internal_trade(
        "u_se", "2030", "SE1", "RI1", "200", "100", project_code="P1"
    )
    svc.confirm_internal_trade("u_ri", ids["trade"])
    return ids
