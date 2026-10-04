"""统计资料接收、校验与版本发布模块的 SQLite 表结构。"""

from __future__ import annotations

STAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS stat_rules_versions (
    rules_version TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_datasets (
    dataset_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    economy TEXT NOT NULL,
    period TEXT NOT NULL,
    methodology TEXT NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT NOT NULL,
    confidentiality TEXT NOT NULL CHECK(confidentiality IN ('public','internal','restricted')),
    source_evidence_json TEXT NOT NULL,
    records_json TEXT NOT NULL,
    records_hash TEXT NOT NULL,
    supersedes TEXT,
    revision_reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('received','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_datasets_period ON stat_datasets(period, status);
CREATE TABLE IF NOT EXISTS stat_validation_runs (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES stat_datasets(dataset_id),
    rules_version TEXT NOT NULL,
    queue_position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','completed')),
    base_release TEXT,
    report_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stat_runs_queue ON stat_validation_runs(status, queue_position);
CREATE TABLE IF NOT EXISTS stat_findings (
    finding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES stat_validation_runs(run_id),
    dataset_id TEXT NOT NULL,
    check_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('error','warning')),
    subject TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','exempted')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_findings_run ON stat_findings(run_id, severity, status);
CREATE TABLE IF NOT EXISTS stat_exemptions (
    exemption_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES stat_findings(finding_id),
    approved_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_review_opinions (
    opinion_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('dataset','candidate')),
    target_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    conclusion TEXT NOT NULL CHECK(conclusion IN ('approve','reject','comment')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_analyst_scopes (
    organization_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(organization_id, scope)
);
CREATE TABLE IF NOT EXISTS stat_derived_indicators (
    code TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    name TEXT NOT NULL,
    formula_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(code, rules_version)
);
CREATE TABLE IF NOT EXISTS stat_candidates (
    candidate_id TEXT PRIMARY KEY,
    report_period TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    coverage TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready','countersigned','published','withdrawn')),
    created_by TEXT NOT NULL,
    creator_org TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_countersigns (
    candidate_id TEXT NOT NULL REFERENCES stat_candidates(candidate_id),
    signer_id TEXT NOT NULL,
    signer_org TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, signer_id)
);
CREATE TABLE IF NOT EXISTS stat_releases (
    version_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES stat_candidates(candidate_id),
    report_period TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    internal_results_json TEXT NOT NULL,
    public_results_json TEXT NOT NULL,
    results_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('published','corrected')),
    correction_of TEXT,
    impact_json TEXT,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_releases_period ON stat_releases(report_period, status, sequence);
CREATE TABLE IF NOT EXISTS stat_release_locks (
    lock_name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
"""


def ensure_schema(connection) -> None:
    """在共享数据库连接上建立统计模块表结构。"""

    connection.executescript(STAT_SCHEMA)
