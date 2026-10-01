"""持久化作业的重启恢复。

关账是多阶段作业（前置检查 → 快照 → 冻结），证据催补落库。进程在任何
时刻重启后，调用 :func:`resume_jobs` 即可把未完成的关账作业继续跑完：
仍然阻断的保持 BLOCKED 并刷新催补清单，条件满足的完成关账。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import GovernanceService


def resume_jobs(service: "GovernanceService") -> list[dict]:
    """续跑全部未完成作业，幂等：可在每次进程启动时调用。"""
    results: list[dict] = []
    for job in service.list_jobs():
        if job["kind"] == "CLOSE_PERIOD" and job["status"] != "DONE":
            results.append(service.execute_close_job(job["id"]))
    service.conn.commit()
    return results


def sweep_reminders(service: "GovernanceService") -> dict:
    """重新扫描有关账作业的期间，把缺失证据转成催补，已补齐的自动核销。

    催补本身持久化，扫描只负责"对账"：重启后不会重复堆积，也不会漏掉
    新出现的缺口。
    """
    created = resolved = 0
    pending = {
        (r["period_code"], r["entity_desc"]): r
        for r in service.list_pending_reminders()
    }
    active_periods = {
        j["period_code"] for j in service.list_jobs()
        if j["kind"] == "CLOSE_PERIOD" and j["status"] != "DONE"
    }
    for period_code in active_periods:
        dataset = service.build_dataset(period_code)
        from .engine import find_blockers

        current = {b["message"]: b["target_org"]
                   for b in find_blockers(dataset)}
        # 阻断仍在 → 确保催补存在；阻断消失 → 核销催补
        for (pc, desc), reminder in list(pending.items()):
            if pc == period_code and desc not in current:
                service.resolve_reminder("SYSTEM", reminder["id"])
                resolved += 1
                pending.pop((pc, desc))
        for message, target_org in current.items():
            if (period_code, message) not in pending:
                service.create_reminder_if_absent(
                    period_code, target_org, message, "关账前置检查未通过"
                )
                created += 1
                pending[(period_code, message)] = {"id": None}
    service.conn.commit()
    return {"created": created, "resolved": resolved}
