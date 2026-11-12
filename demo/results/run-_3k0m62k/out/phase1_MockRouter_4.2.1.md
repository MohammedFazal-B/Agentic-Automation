# Phase 1 — Precondition Verification Plan

**Device:** MockRouter  |  **Firmware:** 4.2.1  |  **Host:** 192.0.2.1  
**Device family:** n/a  |  **Job ID:** `JOB-MockRouter-C00-S00-PRECONDITION-20261112T082110Z-E38B2B`  
**Generated:** 2026-11-12T08:21:10.882+00:00

## 1. What the approved report required

_No explicit protocol/interface preconditions were declared._

## 2. Objectives of this phase

1. Confirm every protocol, port and interface required by the approved report is reachable on this DUT.
2. Automatically remediate any disabled but required management service.
3. Record any capability that this firmware does not implement as Not Supported.
4. Produce a human-reviewable preconditions plan before any test traffic is generated.

## 3. Live probe results

| Requirement | Target | Method / exact command | Observed | Status | Remediation applied |
|---|---|---|---|---|---|

## 6. Interfaces, ports and protocols actually used

| Layer | Identifier | Port | Credential reference | Notes |
|---|---|---|---|---|
| Management GUI | https://192.0.2.1 | 443 | placeholder | Google Chrome via Playwright MCP |
| Management CLI | ssh 192.0.2.1 | 22 | placeholder | SSHv2 only; telnet prohibited by policy |
| Console | serial / local | 0 | n/a | Used only for out-of-band remediation when SSH is disabled |

## 7. Exit criteria for this phase

- Every **required** precondition is `reachable` or `remediated`.
- Any absent capability is explicitly marked `not_supported` with evidence.
- No further precondition work is needed before Phase 2 begins.
