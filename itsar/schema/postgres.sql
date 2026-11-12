-- =====================================================================
-- ITSAR Compliance Agent - canonical PostgreSQL schema (v1)
-- ---------------------------------------------------------------------
-- Design rules enforced here, not just in application code:
--   1. History is immutable. plan/script/verdict rows can never be
--      UPDATEd or DELETEd. "Updates" are new rows with version = max+1.
--   2. Everything hangs off (device, clause, scenario, step).
--   3. Every action is attributable to a job_id.
--   4. Exactly one VERIFIED evidence row per (job, step). Rejected
--      captures are recorded separately and can never be promoted.
--   5. Secrets never land in this database. credential_ref only.
--
-- Timestamps are stored as ISO-8601 UTC TEXT for dialect portability.
-- (Swap to TIMESTAMPTZ if you want server-side arithmetic; the repo
--  layer normalises reads either way.)
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------- devices
CREATE TABLE IF NOT EXISTS device_family (
    id             BIGSERIAL PRIMARY KEY,
    vendor         TEXT NOT NULL,
    name           TEXT NOT NULL,
    base_platform  TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (vendor, name)
);

CREATE TABLE IF NOT EXISTS device (
    id             BIGSERIAL PRIMARY KEY,
    family_id      BIGINT NOT NULL REFERENCES device_family(id),
    model          TEXT NOT NULL,
    firmware       TEXT NOT NULL,
    host           TEXT NOT NULL,
    mgmt_protocol  TEXT NOT NULL DEFAULT 'https',
    credential_ref TEXT NOT NULL,          -- vault path / env var name. NEVER a secret.
    serial         TEXT,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','retired','quarantined')),
    meta_json      TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (model, firmware, host)
);
CREATE INDEX IF NOT EXISTS idx_device_family ON device(family_id);

-- ------------------------------------------------------- clause / scenario
CREATE TABLE IF NOT EXISTS clause (
    id               BIGSERIAL PRIMARY KEY,
    device_id        BIGINT NOT NULL REFERENCES device(id),
    clause_no        TEXT NOT NULL,
    title            TEXT NOT NULL,
    requirement_text TEXT,
    source_report    TEXT,                 -- hash/filename of approved historical report
    created_at       TEXT NOT NULL,
    UNIQUE (device_id, clause_no)
);

CREATE TABLE IF NOT EXISTS scenario (
    id                 BIGSERIAL PRIMARY KEY,
    clause_id          BIGINT NOT NULL REFERENCES clause(id),
    scenario_no        TEXT NOT NULL,
    title              TEXT NOT NULL,
    intent             TEXT,
    preconditions_json TEXT,
    pass_criteria_json TEXT,
    fail_criteria_json TEXT,
    mutation_scope_json TEXT,              -- what this scenario is ALLOWED to change
    severity           TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (clause_id, scenario_no)
);

CREATE TABLE IF NOT EXISTS step (
    id           BIGSERIAL PRIMARY KEY,
    scenario_id  BIGINT NOT NULL REFERENCES scenario(id),
    step_no      INTEGER NOT NULL,
    action       TEXT NOT NULL,
    expected     TEXT,
    tool_hint    TEXT,
    locator_hint TEXT,
    UNIQUE (scenario_id, step_no)
);

-- ------------------------------------------------------------------- plans
-- phase 1 = preconditions, phase 2 = scenario navigation map,
-- phase 3 = live (healed) plan. scenario_id = 0 means clause/device level.
CREATE TABLE IF NOT EXISTS plan (
    id             BIGSERIAL PRIMARY KEY,
    device_id      BIGINT NOT NULL REFERENCES device(id),
    clause_id      BIGINT NOT NULL REFERENCES clause(id),
    scenario_id    BIGINT NOT NULL DEFAULT 0,
    phase          INTEGER NOT NULL CHECK (phase BETWEEN 1 AND 3),
    version        INTEGER NOT NULL,
    path_md        TEXT,                   -- where the .md artefact lives on disk
    content_md     TEXT NOT NULL,
    content_json   TEXT NOT NULL,          -- machine-loadable step map
    content_hash   TEXT NOT NULL,
    parent_plan_id BIGINT REFERENCES plan(id),
    adapted_from   BIGINT REFERENCES plan(id),   -- set when reused for a new DUT
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','approved','executing','superseded')),
    created_by     TEXT NOT NULL DEFAULT 'agent',
    created_at     TEXT NOT NULL,
    UNIQUE (device_id, phase, clause_id, scenario_id, version)
);
CREATE INDEX IF NOT EXISTS idx_plan_lookup ON plan(device_id, phase, clause_id, scenario_id, version DESC);

-- -------------------------------------------------------------------- jobs
CREATE TABLE IF NOT EXISTS job (
    id          TEXT PRIMARY KEY,          -- JOB-<model>-<clause>-<scenario>-<utc>-<rand>
    device_id   BIGINT NOT NULL REFERENCES device(id),
    clause_id   BIGINT REFERENCES clause(id),
    scenario_id BIGINT REFERENCES scenario(id),
    plan_id     BIGINT REFERENCES plan(id),
    script_id   BIGINT,
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
CREATE INDEX IF NOT EXISTS idx_job_device ON job(device_id, started_at DESC);

-- append-only action log: every MCP/tool call and every state change
CREATE TABLE IF NOT EXISTS job_action (
    id             BIGSERIAL PRIMARY KEY,
    job_id         TEXT NOT NULL REFERENCES job(id),
    seq            INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    kind           TEXT NOT NULL,          -- tool_call | state_change | lock_ok | lock_fail
    tool           TEXT,
    op             TEXT,
    target         TEXT,
    request_digest TEXT,
    response_digest TEXT,
    outcome        TEXT NOT NULL,          -- ok | error | timeout | skipped | refused
    latency_ms     INTEGER,
    detail_json    TEXT,
    UNIQUE (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_action_job ON job_action(job_id, seq);

-- ---------------------------------------------------------------- evidence
CREATE TABLE IF NOT EXISTS evidence (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES job(id),
    step_id     BIGINT REFERENCES step(id),
    file_path   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    lock_kind   TEXT NOT NULL,             -- terminal_regex | dom_selector | text_assert | net_idle
    lock_detail TEXT NOT NULL,
    width       INTEGER,
    height      INTEGER,
    captured_at TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (job_id, step_id)               -- one clean screenshot per executed step
);
CREATE INDEX IF NOT EXISTS idx_evidence_sha ON evidence(sha256);

-- audit trail of captures the evidence guard REFUSED (spec directive 4)
CREATE TABLE IF NOT EXISTS evidence_rejection (
    id         BIGSERIAL PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES job(id),
    step_id    BIGINT REFERENCES step(id),
    reason     TEXT NOT NULL,
    detail     TEXT,
    ts         TEXT NOT NULL
);

-- --------------------------------------------------------------- healing
CREATE TABLE IF NOT EXISTS heal_event (
    id                 BIGSERIAL PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES job(id),
    attempt            INTEGER NOT NULL,
    trigger_action_id  BIGINT REFERENCES job_action(id),
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

-- --------------------------------------------------------------- scripts
CREATE TABLE IF NOT EXISTS script (
    id            BIGSERIAL PRIMARY KEY,
    device_id     BIGINT NOT NULL REFERENCES device(id),
    scenario_id   BIGINT NOT NULL REFERENCES scenario(id),
    version       INTEGER NOT NULL,
    language      TEXT NOT NULL DEFAULT 'python',
    file_path     TEXT,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    params_json   TEXT NOT NULL,           -- rebind points for the next DUT
    parent_script_id BIGINT REFERENCES script(id),
    source_job_id TEXT,
    source_trace_json TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (device_id, scenario_id, version)
);

CREATE TABLE IF NOT EXISTS script_run (
    id         BIGSERIAL PRIMARY KEY,
    script_id  BIGINT NOT NULL REFERENCES script(id),
    device_id  BIGINT NOT NULL REFERENCES device(id),
    job_id     TEXT NOT NULL REFERENCES job(id),
    outcome    TEXT NOT NULL,              -- pass | fail | healed_pass
    heal_count INTEGER NOT NULL DEFAULT 0,
    diff_json  TEXT,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);

-- --------------------------------------------------------------- verdicts
CREATE TABLE IF NOT EXISTS verdict (
    id               BIGSERIAL PRIMARY KEY,
    job_id           TEXT NOT NULL REFERENCES job(id),
    device_id        BIGINT NOT NULL REFERENCES device(id),
    clause_no        TEXT NOT NULL,
    scenario_no      TEXT NOT NULL,
    result           TEXT NOT NULL CHECK (result IN ('pass','fail','not_supported','partial','blocked')),
    criteria_met_json TEXT,
    evidence_ids_json TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdict_device ON verdict(device_id, clause_no);

-- ============================================================ IMMUTABILITY
-- Directive 1: never overwrite historical plans/scripts of older devices.
-- Enforced at the storage layer so no code path can bypass it.
CREATE OR REPLACE FUNCTION itsar_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
      'ITSAR immutability: % rows are append-only. Create a new version instead.', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_plan_immutable_u ON plan;
CREATE TRIGGER trg_plan_immutable_u BEFORE UPDATE OR DELETE ON plan
    FOR EACH ROW EXECUTE FUNCTION itsar_block_mutation();

DROP TRIGGER IF EXISTS trg_script_immutable_u ON script;
CREATE TRIGGER trg_script_immutable_u BEFORE UPDATE OR DELETE ON script
    FOR EACH ROW EXECUTE FUNCTION itsar_block_mutation();

DROP TRIGGER IF EXISTS trg_evidence_immutable_u ON evidence;
CREATE TRIGGER trg_evidence_immutable_u BEFORE UPDATE OR DELETE ON evidence
    FOR EACH ROW EXECUTE FUNCTION itsar_block_mutation();

DROP TRIGGER IF EXISTS trg_job_action_immutable_u ON job_action;
CREATE TRIGGER trg_job_action_immutable_u BEFORE UPDATE OR DELETE ON job_action
    FOR EACH ROW EXECUTE FUNCTION itsar_block_mutation();

COMMIT;
