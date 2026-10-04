"""统计资料接收、校验与版本发布服务的 SQLite 表结构。

所有运行状态（校验队列位置、发布锁、候选版本、证据快照）都持久化在
SQLite 中，服务重启后队列位置与发布锁保持不变。
"""

from __future__ import annotations


STATISTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stat_rule_versions (
    rule_version TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    allowed_methodologies_json TEXT NOT NULL,
    expected_economies_json TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    coverage_threshold TEXT NOT NULL,
    consistency_tolerance TEXT NOT NULL,
    required_cosignatures INTEGER NOT NULL CHECK(required_cosignatures >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_fx_rates (
    rule_version TEXT NOT NULL REFERENCES stat_rule_versions(rule_version),
    currency TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    rate_to_base TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_version, currency, effective_from)
);
CREATE TABLE IF NOT EXISTS stat_class_mappings (
    rule_version TEXT NOT NULL REFERENCES stat_rule_versions(rule_version),
    source_category TEXT NOT NULL,
    target_category TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_version, source_category, effective_from)
);
CREATE TABLE IF NOT EXISTS stat_economies (
    economy_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    group_code TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_datasets (
    dataset_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    economy_code TEXT NOT NULL,
    period TEXT NOT NULL,
    methodology TEXT NOT NULL,
    currency TEXT NOT NULL,
    confidentiality TEXT NOT NULL,
    source_evidence TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    revision_of TEXT,
    revision_reason TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_datasets_cell
    ON stat_datasets(economy_code, period, methodology);
CREATE TABLE IF NOT EXISTS stat_validation_runs (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES stat_datasets(dataset_id),
    rule_version TEXT NOT NULL,
    queue_position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    attempt INTEGER NOT NULL DEFAULT 0,
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_stat_validation_queue
    ON stat_validation_runs(status, queue_position);
CREATE TABLE IF NOT EXISTS stat_validation_findings (
    finding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES stat_validation_runs(run_id),
    check_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('error', 'warning')),
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_findings_run ON stat_validation_findings(run_id);
CREATE TABLE IF NOT EXISTS stat_normalized_observations (
    run_id TEXT NOT NULL REFERENCES stat_validation_runs(run_id),
    obs_index INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    economy_code TEXT NOT NULL,
    period TEXT NOT NULL,
    methodology TEXT NOT NULL,
    indicator TEXT NOT NULL,
    value_base TEXT NOT NULL,
    gap INTEGER NOT NULL CHECK(gap IN (0, 1)),
    confidentiality TEXT NOT NULL,
    PRIMARY KEY (run_id, obs_index)
);
CREATE TABLE IF NOT EXISTS stat_exemptions (
    exemption_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL UNIQUE REFERENCES stat_validation_findings(finding_id),
    reason TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_reviews (
    review_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('dataset', 'candidate')),
    target_id TEXT NOT NULL,
    opinion TEXT NOT NULL CHECK(opinion IN ('approve', 'reject', 'comment')),
    comment TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_candidates (
    candidate_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'published', 'withdrawn')),
    coverage TEXT,
    correction_of_release_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_cosignatures (
    candidate_id TEXT NOT NULL REFERENCES stat_candidates(candidate_id),
    actor_id TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (candidate_id, actor_id)
);
CREATE TABLE IF NOT EXISTS stat_releases (
    release_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES stat_candidates(candidate_id),
    version_number INTEGER NOT NULL,
    period TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    corrects_release_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('published', 'superseded')),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_release_inputs (
    release_id TEXT NOT NULL REFERENCES stat_releases(release_id),
    dataset_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (release_id, dataset_id)
);
CREATE TABLE IF NOT EXISTS stat_release_derived (
    release_id TEXT NOT NULL REFERENCES stat_releases(release_id),
    indicator_id TEXT NOT NULL,
    formula_hash TEXT NOT NULL,
    PRIMARY KEY (release_id, indicator_id)
);
CREATE TABLE IF NOT EXISTS stat_release_metrics (
    release_id TEXT NOT NULL REFERENCES stat_releases(release_id),
    indicator TEXT NOT NULL,
    scope TEXT NOT NULL,
    value TEXT NOT NULL,
    gap INTEGER NOT NULL CHECK(gap IN (0, 1)),
    all_public INTEGER NOT NULL CHECK(all_public IN (0, 1)),
    PRIMARY KEY (release_id, indicator, scope)
);
CREATE TABLE IF NOT EXISTS stat_corrections (
    correction_id TEXT PRIMARY KEY,
    base_release_id TEXT NOT NULL REFERENCES stat_releases(release_id),
    candidate_id TEXT NOT NULL REFERENCES stat_candidates(candidate_id),
    new_release_id TEXT,
    reason TEXT NOT NULL,
    affected_economies_json TEXT NOT NULL,
    affected_indicators_json TEXT NOT NULL,
    affected_charts_json TEXT NOT NULL,
    affected_conclusions_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_derived_indicators (
    indicator_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    formula_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stat_analyst_grants (
    actor_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('indicator')),
    scope_value TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, scope_type, scope_value)
);
CREATE TABLE IF NOT EXISTS stat_locks (
    lock_name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
"""


def ensure_schema(connection) -> None:
    """在既有数据库连接上建立统计服务所需的表。"""

    connection.executescript(STATISTICS_SCHEMA)
