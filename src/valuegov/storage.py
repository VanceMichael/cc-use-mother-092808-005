"""SQLite 持久层：结构定义、连接与可重入事务。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS principals (
    name        TEXT PRIMARY KEY,
    roles       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_units (
    unit_id             TEXT PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    unit_type           TEXT NOT NULL,
    parent_id           TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    retired_from_period TEXT,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reorg_events (
    event_id         TEXT PRIMARY KEY,
    from_unit_id     TEXT NOT NULL,
    to_unit_id       TEXT NOT NULL,
    effective_period TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industry_categories (
    category_id            TEXT PRIMARY KEY,
    code                   TEXT NOT NULL UNIQUE,
    name                   TEXT NOT NULL,
    is_strategic_emerging  INTEGER NOT NULL DEFAULT 0,
    parent_code            TEXT,
    status                 TEXT NOT NULL DEFAULT 'ACTIVE',
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS formula_versions (
    version    INTEGER PRIMARY KEY,
    definition TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    period      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    snapshot_id TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    period          TEXT NOT NULL UNIQUE,
    formula_version INTEGER NOT NULL,
    org_scope       TEXT NOT NULL,
    targets         TEXT NOT NULL,
    results         TEXT NOT NULL,
    closed_seq      INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id          TEXT PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    owner_unit_id       TEXT NOT NULL,
    category_id         TEXT,
    pending_category_id TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    parent_project_id   TEXT,
    split_group_id      TEXT,
    split_from_period   TEXT,
    split_weight        REAL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocation_rules (
    rule_id          TEXT PRIMARY KEY,
    rule_set_id      TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    unit_id          TEXT NOT NULL,
    weight           REAL NOT NULL,
    basis            TEXT NOT NULL,
    effective_period TEXT NOT NULL,
    superseded_by    TEXT,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investment_batches (
    batch_id       TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    period         TEXT NOT NULL,
    amount_minor   INTEGER NOT NULL,
    funding_source TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    void           INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1,
    supersedes_id  TEXT,
    superseded_by  TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rd_expenses (
    expense_id             TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
    period                 TEXT NOT NULL,
    amount_minor           INTEGER NOT NULL,
    expense_kind           TEXT NOT NULL,
    is_basic_research      INTEGER NOT NULL DEFAULT 0,
    basic_research_basis   TEXT,
    pending_basic_research INTEGER NOT NULL DEFAULT 0,
    void                   INTEGER NOT NULL DEFAULT 0,
    version                INTEGER NOT NULL DEFAULT 1,
    supersedes_id          TEXT,
    superseded_by          TEXT,
    created_by             TEXT NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transformations (
    transformation_id  TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL,
    period             TEXT NOT NULL,
    revenue_minor      INTEGER NOT NULL DEFAULT 0,
    value_added_minor  INTEGER NOT NULL DEFAULT 0,
    description        TEXT NOT NULL DEFAULT '',
    void               INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    supersedes_id      TEXT,
    superseded_by      TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS public_service_contributions (
    contribution_id   TEXT PRIMARY KEY,
    unit_id           TEXT NOT NULL,
    period            TEXT NOT NULL,
    service_type      TEXT NOT NULL,
    value_minor       INTEGER NOT NULL,
    beneficiary_scope TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    void              INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    supersedes_id     TEXT,
    superseded_by     TEXT,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_transactions (
    txn_id            TEXT PRIMARY KEY,
    period            TEXT NOT NULL,
    seller_unit_id    TEXT NOT NULL,
    buyer_unit_id     TEXT NOT NULL,
    project_id        TEXT,
    amount_minor      INTEGER NOT NULL,
    value_added_minor INTEGER NOT NULL DEFAULT 0,
    description       TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'OPEN',
    void              INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    supersedes_id     TEXT,
    superseded_by     TEXT,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS elimination_runs (
    run_id     TEXT PRIMARY KEY,
    period     TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS elimination_entries (
    entry_id          TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    txn_id            TEXT NOT NULL,
    amount_minor      INTEGER NOT NULL,
    value_added_minor INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id   TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    period        TEXT,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    submitted_by  TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    period        TEXT NOT NULL,
    changes       TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    new_entity_id TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT
);

CREATE TABLE IF NOT EXISTS plan_targets (
    target_id     TEXT PRIMARY KEY,
    metric        TEXT NOT NULL,
    period        TEXT NOT NULL,
    target_value  REAL NOT NULL,
    superseded_by TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id    TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'RECEIVED',
    received_by TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_items (
    item_id        TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    item_type      TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    status         TEXT NOT NULL,
    applied_entity TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_import_items_key ON import_items(idem_key);

CREATE TABLE IF NOT EXISTS review_tasks (
    review_id   TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    resolution  TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_requests (
    request_id   TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    unit_id      TEXT NOT NULL,
    description  TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    ref_id      TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(kind, ref_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    job_type   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'PENDING',
    attempts   INTEGER NOT NULL DEFAULT 0,
    result     TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class Store:
    """单连接 SQLite 存储，事务可重入，读写均在锁内执行。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def tx(self):
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield self
            finally:
                self._local.depth = depth
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield self
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                self._local.depth = 0

    def q1(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def qa(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def run(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, args)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
