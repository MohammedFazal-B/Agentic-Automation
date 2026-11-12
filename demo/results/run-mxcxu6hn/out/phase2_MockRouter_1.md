# Phase 2 — Scenario Execution Plan

**Device:** MockRouter (4.2.1)  |  **Host:** 192.0.2.1  
**Clause:** 1 — Synthetic SSH check  |  **Scenario:** 1 — Inspect SSH  
**Dry-run job:** `JOB-MockRouter-1-1-DRY_RUN-20261112T094510Z-BF907E`

## 1. Intent behind this scenario

Inspect SSH

## 2. Pass criteria (from approved report)

- [ ] ssh protocol version 2.0

## 3. Fail criteria

- _none declared_

## 4. Tool binding and navigation map

| # | Step intent | Tool / MCP | Operation | Arguments | State lock (success proof) | Evidence |
|---|---|---|---|---|---|---|
| 1 | show ssh | `cli` | `show` | `{"command": "show ssh"}` | terminal_regex: `(?i)ssh` | yes |

## 5. Dry-run findings (non-destructive)

- Step 1: dry run: `show ssh` accepted, 2 lines returned

## 6. Mutation scope

This plan may only change: _nothing (read-only scenario)_.

## 7. Evidence policy for this scenario

A screenshot is captured only after the **State lock** column proves success. Error pages, missing-dependency warnings, auth walls and loading frames are refused by the evidence guard and logged as rejections — never attached to the report.

## 8. Post-execution

- Evaluate pass/fail criteria strictly from captured evidence.
- On success: emit the scenario as a **v1** executable script bound to this DUT.
- On a plan defect: heal, store the healed plan as the next version, resume.
