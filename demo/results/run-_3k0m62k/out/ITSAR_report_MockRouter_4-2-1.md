# ITSAR Compliance Assessment Report

| Field | Value |
|---|---|
| Device under test | **MockRouter** |
| Firmware under test | 4.2.1 |
| Device family | Router |
| Management address | https://192.0.2.1 |
| Credential reference | `placeholder` (secret not stored) |
| Assessment standard | ITSAR placeholder |
| Standard version | n/a |
| Assessment basis | Placeholder input; exploratory automated run (not an approved assessment) |
| Report generated | 2026-11-12T08:21:10.947+00:00 |
| Report ID | ITSAR-MockRouter-4.2.1 |

## 1. Executive summary

1 scenario(s) were executed against **MockRouter** running firmware **4.2.1**. **1** passed, **0** failed, **0** were recorded as not supported by this firmware. Automated compliance for the tested scope is **100.0%**. This is a heuristic prototype result, not a compliance determination.

| Result | Scenarios | Share |
|---|---|---|
| pass | 1 | 100% |

PROTOTYPE ONLY: synthetic inputs and mock results must not be used as evidence of ITSAR compliance. Criteria matching is heuristic and requires expert validation.

## 2. Scope and methodology

- **Phase 1 — preconditions.** The prototype attempted catalogue-supported probes and remediation.
- **Phase 2 — dry run.** Navigation paths and command forms were discovered non-destructively and validated before any state-changing action.
- **Phase 3 — execution.** Each step ran under a unique Job ID with a deterministic state lock; a step's evidence was captured only after its lock was satisfied.
- **Phase 4 — reuse.** Family reuse is experimental; consult the job log for what actually ran.
- **Evidence policy.** Screenshots are refused unless the step's success was proven. Error pages, dependency warnings, authentication walls, loading frames and duplicate frames are logged as rejections and never appear in this report.

## 3. Results by clause

| Clause | Scenario | Verdict | Evidence | Basis |
|---|---|---|---|---|
| 1 | 1 | **PASS** | 1 artefact(s) | 1 pass criteria evaluated, 1 met |

## 4. Detailed findings

### Clause 1 / 1 — Inspect SSH

**Verdict:** PASS  ·  **Requirement:** Demonstration only; not an actual ITSAR requirement.

#### Pass criteria

| Criterion | Met | Matched evidence terms |
|---|---|---|
| ssh protocol version 2.0 | yes | protocol, version |

#### Executed steps

| # | Step | Tool / op | Outcome | Heals |
|---|---|---|---|---|
| 1 | show ssh | `cli/show` | pass | 0 |

#### Evidence

![Job JOB-MockRouter-1-1-EXECUTE-20261112T082110Z-2430AF · captured 2026-11-12T08:21:10.941+00:00 · lock terminal_regex (/(?i)ssh/ matched: 'SSH') · sha256 3a5c75a59dd1f358…](step01_3a5c75a59d.png)
*Job JOB-MockRouter-1-1-EXECUTE-20261112T082110Z-2430AF · captured 2026-11-12T08:21:10.941+00:00 · lock terminal_regex (/(?i)ssh/ matched: 'SSH') · sha256 3a5c75a59dd1f358…*

## 5. Self-healing and deviations

No plan deviations occurred during this assessment.

## 6. Automation assets produced

| Scenario | Device | Firmware | Version | Origin | File |
|---|---|---|---|---|---|
| 1 | MockRouter | 4.2.1 | **v1** | authored | `scenario_1_v1_MockRouter.py` |

## 7. Evidence integrity annex

| Scenario | Artefact | Lock verified | Lock detail | SHA-256 (prefix) |
|---|---|---|---|---|
| 1 | step01_3a5c75a59d.png | terminal_regex | /(?i)ssh/ matched: 'SSH' | `3a5c75a59dd1f358ff417afb…` |

---

## 8. Declaration

The results in this report were produced by automated execution against MockRouter (firmware 4.2.1) using the Job IDs recorded in the assessment database. Every verdict is traceable to a job, a plan version, and stored evidence where available. Independent verification is required.

| Role | Name | Date |
|---|---|---|
| Assessor |  |  |
| Reviewer |  |  |
| Approver |  |  |
