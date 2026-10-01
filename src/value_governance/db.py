"""SQLite 持久化层：建表脚本与连接管理。

所有金额以两位小数字符串保存（可审计、无浮点误差），份额以四位小数
保存。关账快照、作业与催补均落库，进程重启后可继续。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- 身份与角色 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN
                   ('DATA_OFFICE','PLANNER','REVIEWER','UNIT_USER')),
    org_code     TEXT REFERENCES organizations(code),
    created_at   TEXT NOT NULL
);

-- 组织边界 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    org_type  TEXT NOT NULL CHECK (org_type IN
                ('GROUP','SECTOR','RESEARCH_INSTITUTE','PUBLIC_SERVICE')),
    parent_code TEXT REFERENCES organizations(code),
    active    INTEGER NOT NULL DEFAULT 1
);

-- 重组事件与映射：源单位的历史贡献按权重平移到目标单位，权重合计必须为 1
CREATE TABLE IF NOT EXISTS reorg_events (
    id             INTEGER PRIMARY KEY,
    effective_period TEXT NOT NULL REFERENCES periods(code),
    kind           TEXT NOT NULL CHECK (kind IN ('MERGE','SPLIT','RENAME','MOVE')),
    description    TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reorg_mappings (
    id        INTEGER PRIMARY KEY,
    event_id  INTEGER NOT NULL REFERENCES reorg_events(id),
    source_org TEXT NOT NULL REFERENCES organizations(code),
    target_org TEXT NOT NULL REFERENCES organizations(code),
    weight    TEXT NOT NULL CHECK (1=1)
);

-- 产业分类目录 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS industry_catalog (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    is_strategic INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1
);

-- 项目与拆分 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    lead_org      TEXT NOT NULL REFERENCES organizations(code),
    industry_code TEXT NOT NULL REFERENCES industry_catalog(code),
    parent_project TEXT REFERENCES projects(code),
    status        TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','SPLIT','CLOSED')),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_splits (
    parent_project   TEXT NOT NULL REFERENCES projects(code),
    child_project    TEXT NOT NULL REFERENCES projects(code),
    weight           TEXT NOT NULL,
    effective_period TEXT NOT NULL REFERENCES periods(code),
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (child_project)
);

CREATE TABLE IF NOT EXISTS project_tasks (
    code         TEXT PRIMARY KEY,
    project_code TEXT NOT NULL REFERENCES projects(code),
    name         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investment_batches (
    code         TEXT PRIMARY KEY,
    project_code TEXT NOT NULL REFERENCES projects(code),
    task_code    TEXT REFERENCES project_tasks(code),
    period_code  TEXT NOT NULL REFERENCES periods(code),
    name         TEXT NOT NULL,
    amount       TEXT NOT NULL
);

-- 协作分摊规则：每个作用域内份额合计必须恰为 1，版本化、只新增不覆盖
CREATE TABLE IF NOT EXISTS allocation_rule_sets (
    id           INTEGER PRIMARY KEY,
    project_code TEXT NOT NULL REFERENCES projects(code),
    task_code    TEXT REFERENCES project_tasks(code),
    batch_code   TEXT REFERENCES investment_batches(code),
    period_code  TEXT NOT NULL REFERENCES periods(code),
    basis        TEXT NOT NULL CHECK (basis IN
                   ('INVESTMENT','CONTRACT','HEADCOUNT','REVENUE','CUSTOM')),
    note         TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_shares (
    rule_set_id INTEGER NOT NULL REFERENCES allocation_rule_sets(id),
    org_code    TEXT NOT NULL REFERENCES organizations(code),
    share       TEXT NOT NULL,
    PRIMARY KEY (rule_set_id, org_code)
);

-- 报告期与目标 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS periods (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'OPEN'
                 CHECK (status IN ('OPEN','CLOSING','CLOSED')),
    prior_period_code TEXT REFERENCES periods(code),
    closed_at  TEXT,
    closed_by  TEXT
);
CREATE TABLE IF NOT EXISTS period_goals (
    period_code    TEXT NOT NULL REFERENCES periods(code),
    metric         TEXT NOT NULL CHECK (metric IN
                     ('STRATEGIC_VA_RATIO','RD_GROWTH','BASIC_RESEARCH_RATIO')),
    target_value   TEXT NOT NULL,   -- 比率类统一存小数（0.15 表示 15%）
    baseline_value TEXT,            -- 战新占比目标为基期值 + 5 个百分点
    note           TEXT NOT NULL DEFAULT '',
    updated_by     TEXT,
    updated_at     TEXT,
    PRIMARY KEY (period_code, metric)
);

-- 贡献记录 ----------------------------------------------------------------
-- 项目产出：营业收入与增加值（抵销前总额）
CREATE TABLE IF NOT EXISTS outputs (
    id              INTEGER PRIMARY KEY,
    period_code     TEXT NOT NULL REFERENCES periods(code),
    project_code    TEXT NOT NULL REFERENCES projects(code),
    task_code       TEXT REFERENCES project_tasks(code),
    batch_code      TEXT REFERENCES investment_batches(code),
    revenue         TEXT NOT NULL,
    value_added     TEXT NOT NULL,
    claimed_strategic INTEGER NOT NULL DEFAULT 0,  -- 申请按战新认定
    evidence_ref    TEXT NOT NULL DEFAULT '',
    reported_by     TEXT NOT NULL,
    reported_org    TEXT NOT NULL REFERENCES organizations(code),
    created_at      TEXT NOT NULL
);

-- 研发费用：basic_claim=1 时必须有已批准的基础研究属性认定
CREATE TABLE IF NOT EXISTS rd_expenses (
    id              INTEGER PRIMARY KEY,
    period_code     TEXT NOT NULL REFERENCES periods(code),
    project_code    TEXT NOT NULL REFERENCES projects(code),
    task_code       TEXT REFERENCES project_tasks(code),
    batch_code      TEXT REFERENCES investment_batches(code),
    amount          TEXT NOT NULL,
    basic_claim     INTEGER NOT NULL DEFAULT 0,
    evidence_ref    TEXT NOT NULL DEFAULT '',
    reported_by     TEXT NOT NULL,
    reported_org    TEXT NOT NULL REFERENCES organizations(code),
    created_at      TEXT NOT NULL
);

-- 成果转化收入与增加值
CREATE TABLE IF NOT EXISTS transformations (
    id              INTEGER PRIMARY KEY,
    period_code     TEXT NOT NULL REFERENCES periods(code),
    project_code    TEXT NOT NULL REFERENCES projects(code),
    batch_code      TEXT REFERENCES investment_batches(code),
    revenue         TEXT NOT NULL,
    value_added     TEXT NOT NULL,
    evidence_ref    TEXT NOT NULL DEFAULT '',
    reported_by     TEXT NOT NULL,
    reported_org    TEXT NOT NULL REFERENCES organizations(code),
    created_at      TEXT NOT NULL
);

-- 公共服务贡献（成本法等计量）
CREATE TABLE IF NOT EXISTS public_services (
    id              INTEGER PRIMARY KEY,
    period_code     TEXT NOT NULL REFERENCES periods(code),
    org_code        TEXT NOT NULL REFERENCES organizations(code),
    project_code    TEXT REFERENCES projects(code),
    amount          TEXT NOT NULL,
    metric_text     TEXT NOT NULL DEFAULT '',
    evidence_ref    TEXT NOT NULL DEFAULT '',
    reported_by     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 集团内部交易：双边确认后才可抵销；DISPUTED/PENDING 阻断关账
CREATE TABLE IF NOT EXISTS internal_trades (
    id              INTEGER PRIMARY KEY,
    period_code     TEXT NOT NULL REFERENCES periods(code),
    project_code    TEXT REFERENCES projects(code),
    seller_org      TEXT NOT NULL REFERENCES organizations(code),
    buyer_org       TEXT NOT NULL REFERENCES organizations(code),
    revenue_amount  TEXT NOT NULL,
    value_added_amount TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                     ('PENDING','MATCHED','DISPUTED')),
    recorded_by     TEXT NOT NULL,
    confirmed_by    TEXT,
    dispute_note    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    CHECK (seller_org <> buyer_org)
);

-- 特殊归类审批：提交人不得批准自己的申请
CREATE TABLE IF NOT EXISTS classification_requests (
    id            INTEGER PRIMARY KEY,
    period_code   TEXT NOT NULL REFERENCES periods(code),
    kind          TEXT NOT NULL CHECK (kind IN
                    ('PROJECT_INDUSTRY','OUTPUT_STRATEGIC','RD_BASIC')),
    target_ref    TEXT NOT NULL,   -- 项目编码或记录行 ID
    claimed_value TEXT NOT NULL,   -- 产业编码 / true / false
    evidence_ref  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
                    ('SUBMITTED','APPROVED','REJECTED')),
    submitted_by  TEXT NOT NULL,
    submitted_org TEXT NOT NULL REFERENCES organizations(code),
    submitted_at  TEXT NOT NULL,
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    review_note   TEXT NOT NULL DEFAULT ''
);

-- 批量导入 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY,
    period_code  TEXT NOT NULL REFERENCES periods(code),
    client_ref   TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN
                   ('RECEIVED','RESEND','CONFLICT_REVIEW','DONE')),
    submitted_by TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    applied_rows INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (period_code, client_ref)
);
CREATE TABLE IF NOT EXISTS import_rows (
    id             INTEGER PRIMARY KEY,
    batch_id       INTEGER NOT NULL REFERENCES import_batches(id),
    line_no        INTEGER NOT NULL,
    row_kind       TEXT NOT NULL CHECK (row_kind IN
                     ('OUTPUT','RD_EXPENSE','TRANSFORMATION',
                      'PUBLIC_SERVICE','INTERNAL_TRADE')),
    dedup_key      TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN
                     ('NEW','DUPLICATE','CONFLICT','REJECTED','APPLIED')),
    conflict_reason TEXT NOT NULL DEFAULT '',
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    UNIQUE (batch_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_import_rows_dedup ON import_rows(dedup_key);

-- 复核队列（冲突材料、内部交易争议）
CREATE TABLE IF NOT EXISTS review_queue (
    id          INTEGER PRIMARY KEY,
    ref_type    TEXT NOT NULL CHECK (ref_type IN
                  ('IMPORT_ROW','TRADE_DISPUTE')),
    ref_id      INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OPEN'
                  CHECK (status IN ('OPEN','RESOLVED')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution  TEXT NOT NULL DEFAULT ''
);

-- 持久化作业：关账多阶段推进，重启后续跑
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('CLOSE_PERIOD','REMINDER_SWEEP')),
    period_code TEXT NOT NULL REFERENCES periods(code),
    status      TEXT NOT NULL CHECK (status IN
                  ('PENDING','RUNNING','DONE','BLOCKED','FAILED')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 证据催补
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    period_code TEXT NOT NULL REFERENCES periods(code),
    target_org  TEXT NOT NULL REFERENCES organizations(code),
    entity_desc TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING','RESOLVED')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);

-- 关账快照：公式版本、组织范围、目标与全部数据随快照冻结
CREATE TABLE IF NOT EXISTS snapshots (
    period_code TEXT NOT NULL REFERENCES periods(code),
    revision    INTEGER NOT NULL DEFAULT 0,
    formula_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'CLOSE' CHECK (kind IN ('CLOSE','CORRECTION')),
    correction_id INTEGER,
    taken_at    TEXT NOT NULL,
    PRIMARY KEY (period_code, revision)
);

-- 更正版本：历史错误只允许通过更正版本修正，双人、不得改公式与组织范围
CREATE TABLE IF NOT EXISTS corrections (
    id          INTEGER PRIMARY KEY,
    period_code TEXT NOT NULL REFERENCES periods(code),
    revision    INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                  ('SUBMITTED','APPROVED','REJECTED','APPLIED')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by  TEXT,
    reviewed_at  TEXT,
    applied_at   TEXT
);
CREATE TABLE IF NOT EXISTS correction_entries (
    id            INTEGER PRIMARY KEY,
    correction_id INTEGER NOT NULL REFERENCES corrections(id),
    section       TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    before_json   TEXT NOT NULL,
    after_json    TEXT NOT NULL
);

-- 审计日志：所有关键动作留痕
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
"""


def open_store(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）治理数据库。"""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
