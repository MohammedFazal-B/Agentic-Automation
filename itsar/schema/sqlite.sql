-- =====================================================================
-- ITSAR Compliance Agent - SQLite dev-mode schema.
-- Mirrors postgres.sql exactly (see that file for design rules).
-- Used only so the harness can be exercised without a Postgres server.
-- Any behavioural difference is a bug: keep the two in lockstep.
-- =====================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS device_family (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor         TEXT NOT NULL,
    name           TEXT NOT NULL,
    base_platform  TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (vendor, name)
);

CREATE TABLE IF NOT EXISTS device (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id      INTEGER NOT NULL REFERENCES device_family(id),
    model          TEXT NOT NULL,
    firmware       TEXT NOT NULL,
    host           TEXT NOT NULL,
    mgmt_protocol  TEXT NOT NULL DEFAULT 'https',
    credential_ref TEXT NOT NULL,
    serial         TEXT,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','retired','quarantined')),
    meta_json      TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (model, firmware, host)
);

CREATE TABLE IF NOT EXISTS clause (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        INTEGER NOT NULL REFERENCES device(id),
    clause_no        TEXT NOT NULL,
    title            TEXT NOT NULL,
    requirement_text TEXT,
    source_report    TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (device_id, clause_no)
);

CREATE TABLE IF NOT EXISTS scenario (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    clause_id          INTEGER NOT NULL REFERENCES clause(id),
    scenario_no        TEXT NOT NULL,
    title              TEXT NOT NULL,
    intent             TEXT,
    preconditions_json TEXT,
    pass_criteria_json TEXT,
    fail_criteria_json TEXT,
    mutation_scope_json TEXT,
    severity           TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (clause_id, scenario_no)
);

CREATE TABLE IF NOT EXISTS step (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id  INTEGER NOT NULL REFERENCES scenario(id),
    step_no      INTEGER NOT NULL,
    action       TEXT NOT NULL,
    expected     TEXT,
    tool_hint    TEXT,
    locator_hint TEXT,
    UNIQUE (scenario_id, step_no)
);

CREATE TABLE IF NOT EXISTS plan (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL REFERENCES device(id),
    clause_id      INTEGER NOT NULL REFERENCES clause(id),
    scenario_id    INTEGER NOT NULL DEFAULT 0,
    phase          INTEGER NOT NULL CHECK (phase BETWEEN 1 AND 3),
    version        INTEGER NOT NULL,
    path_md        TEXT,
    content_md     TEXT NOT NULL,
    content_json   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    parent_plan_id INTEGER REFERENCES plan(id),
    adapted_from   INTEGER REFERENCES plan(id),
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','approved','executing','superseded')),
    created_by     TEXT NOT NULL DEFAULT 'agent',
    created_at     TEXT NOT NULL,
    UNIQUE (device_id, phase, clause_id, scenario_id, version)
);

CREATE TABLE IF NOT EXISTS job (
    id          TEXT PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    clause_id   INTEGER REFERENCES clause(id),
    scenario_id INTEGER REFERENCES scenario(id),
    plan_id     INTEGER REFERENCES plan(id),
    script_id   INTEGER,
    kind        TEXT NOT NULL CHECK (kind IN ('precondition','dry_run','execute','reuse','script_run')),
    state       TEXT NOT NULL DEFAULT 'created'
                CHECK (state IN ('created','probing','running','healing','passed',
                                 'failed','blocked','not_supported','aborted')),
    verdict     TEXT CHECK (verdict IN ('pass','fail','not_supported','partial')),
    summary     TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    meta_json   TEXT
);

CREATE TABLE IF NOT EXISTS job_action (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL REFERENCES job(id),
    seq            INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    kind           TEXT NOT NULL,
    tool           TEXT,
    op             TEXT,
    target         TEXT,
    request_digest TEXT,
    response_digest TEXT,
    outcome        TEXT NOT NULL,
    latency_ms     INTEGER,
    detail_json    TEXT,
    UNIQUE (job_id, seq)
);

CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES job(id),
    step_id     INTEGER REFERENCES step(id),
    file_path   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    lock_kind   TEXT NOT NULL,
    lock_detail TEXT NOT NULL,
    width       INTEGER,
    height      INTEGER,
    captured_at TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (job_id, step_id)
);

CREATE TABLE IF NOT EXISTS evidence_rejection (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES job(id),
    step_id    INTEGER REFERENCES step(id),
    reason     TEXT NOT NULL,
    detail     TEXT,
    ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heal_event (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL REFERENCES job(id),
    attempt            INTEGER NOT NULL,
    trigger_action_id  INTEGER REFERENCES job_action(id),
    diagnosis          TEXT NOT NULL,
    strategy           TEXT NOT NULL,
    patch_json         TEXT NOT NULL,
    plan_version_before INTEGER,
    plan_version_after  INTEGER,
    applied            INTEGER NOT NULL DEFAULT 0,
    approval_required  INTEGER NOT NULL DEFAULT 0,
    outcome            TEXT,
    ts                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS script (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL REFERENCES device(id),
    scenario_id   INTEGER NOT NULL REFERENCES scenario(id),
    version       INTEGER NOT NULL,
    language      TEXT NOT NULL DEFAULT 'python',
    file_path     TEXT,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    parent_script_id INTEGER REFERENCES script(id),
    source_job_id TEXT,
    source_trace_json TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (device_id, scenario_id, version)
);

CREATE TABLE IF NOT EXISTS script_run (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id  INTEGER NOT NULL REFERENCES script(id),
    device_id  INTEGER NOT NULL REFERENCES device(id),
    job_id     TEXT NOT NULL REFERENCES job(id),
    outcome    TEXT NOT NULL,
    heal_count INTEGER NOT NULL DEFAULT 0,
    diff_json  TEXT,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS verdict (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL REFERENCES job(id),
    device_id        INTEGER NOT NULL REFERENCES device(id),
    clause_no        TEXT NOT NULL,
    scenario_no      TEXT NOT NULL,
    result           TEXT NOT NULL CHECK (result IN ('pass','fail','not_supported','partial','blocked')),
    criteria_met_json TEXT,
    evidence_ids_json TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL
);

-- ------------------------------------------------------------ immutability
CREATE TRIGGER IF NOT EXISTS trg_plan_immutable_u BEFORE UPDATE ON plan
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: plan rows are append-only. Create a new version instead.'); END;

CREATE TRIGGER IF NOT EXISTS trg_plan_immutable_d BEFORE DELETE ON plan
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: plan rows are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS trg_script_immutable_u BEFORE UPDATE ON script
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: script rows are append-only. Create a new version instead.'); END;

CREATE TRIGGER IF NOT EXISTS trg_script_immutable_d BEFORE DELETE ON script
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: script rows are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_immutable_u BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: evidence rows are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS trg_job_action_immutable_u BEFORE UPDATE ON job_action
BEGIN SELECT RAISE(ABORT, 'ITSAR immutability: job_action rows are append-only.'); END;
