"""Ingest an approved historical ITSAR report (JSON) and normalise it.

The upstream parser (Haiku) is assumed to have already turned the PDF/DOCX into
JSON. Real reports are inconsistent, so this module is deliberately tolerant of
field aliases and never depends on a single key name.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..repo import Repo, utcnow


@dataclass
class ApprovedReport:
    meta: dict = field(default_factory=dict)
    dut: dict = field(default_factory=dict)
    preconditions: dict = field(default_factory=dict)
    objectives: list[str] = field(default_factory=list)
    clauses: list[dict] = field(default_factory=list)
    source_hash: str = ""

    def scenarios(self):
        for cl in self.clauses:
            for sc in cl.get("scenarios", []):
                yield cl, sc


def _first(d: dict, keys: list[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def parse_report(source: str | Path | dict) -> ApprovedReport:
    if isinstance(source, (str, Path)):
        raw_text = Path(source).read_text(encoding="utf-8")
        data = json.loads(raw_text)
    else:
        data = source
        raw_text = json.dumps(data, sort_keys=True)

    meta = _first(data, ["report_meta", "metadata", "report"], {}) or {}
    dut = _first(data, ["dut", "device", "device_under_test"], {}) or {}
    pre = _first(data, ["preconditions", "pre_conditions", "environment"], {}) or {}

    # preconditions: accept flat lists or {"protocols":[...],"interfaces":[...]}
    if isinstance(pre, list):
        pre = {"protocols": pre}
    for key in ("protocols", "interfaces", "services"):
        if key not in pre:
            alt = _first(pre, [key.rstrip("s"), key + "_list"], None)
            if alt:
                pre[key] = alt

    objectives = _first(pre, ["objectives", "goals"], []) or \
        _first(data, ["objectives"], []) or []

    clauses_raw = _first(data, ["clauses", "requirements", "sections", "test_clauses"], []) or []
    clauses: list[dict] = []
    for cl in clauses_raw:
        clause = {
            "clause_no": str(_first(cl, ["clause_no", "clause", "id", "number"], "")),
            "title": _first(cl, ["title", "name", "requirement_title"], ""),
            "requirement_text": _first(cl, ["requirement_text", "requirement", "text", "description"], ""),
            "scenarios": [],
        }
        for sc in _first(cl, ["scenarios", "test_scenarios", "tests", "cases"], []) or []:
            steps = _first(sc, ["steps", "execution_steps", "test_steps", "procedure"], []) or []
            norm_steps = []
            for i, st in enumerate(steps, 1):
                if isinstance(st, str):
                    norm_steps.append({"step_no": i, "action": st, "expected": "",
                                       "tool_hint": "", "locator_hint": ""})
                    continue
                norm_steps.append({
                    "step_no": int(_first(st, ["step_no", "no", "id", "index"], i)),
                    "action": _first(st, ["action", "step", "description", "instruction"], ""),
                    "expected": _first(st, ["expected", "expected_result", "outcome"], ""),
                    "tool_hint": str(_first(st, ["tool", "tool_hint", "channel"], "")),
                    "locator_hint": _first(st, ["locator", "locator_hint", "command", "path", "menu"], ""),
                    "op": _first(st, ["op", "operation"], ""),
                    "args": _first(st, ["args", "arguments"], {}),
                })
            clause["scenarios"].append({
                "scenario_no": str(_first(sc, ["scenario_no", "scenario", "id", "number"], "")),
                "title": _first(sc, ["title", "name"], ""),
                "intent": _first(sc, ["intent", "objective", "purpose", "description"], ""),
                "preconditions": _first(sc, ["preconditions"], []),
                "pass_criteria": _first(sc, ["pass_criteria", "pass", "expected_result"], []) or [],
                "fail_criteria": _first(sc, ["fail_criteria", "fail"], []) or [],
                "mutation_scope": _first(sc, ["mutation_scope", "mutations", "allowed_changes"], []) or [],
                "severity": _first(sc, ["severity", "criticality"], ""),
                "steps": norm_steps,
            })
        clauses.append(clause)

    return ApprovedReport(
        meta=meta, dut=dut, preconditions=pre, objectives=objectives, clauses=clauses,
        source_hash=hashlib.sha256(raw_text.encode()).hexdigest()[:16],
    )


def ingest(repo: Repo, report: ApprovedReport, device_spec: dict) -> dict:
    """Create/attach device, clauses, scenarios and steps. Idempotent per device."""
    device = repo.ensure_device(device_spec)
    source = f"{report.meta.get('report_id', 'approved-report')}#{report.source_hash}"
    mapping = {"device": device, "clauses": {}, "scenarios": {}}
    for cl in report.clauses:
        clause_id = repo.upsert_clause(device["id"], cl, source)
        mapping["clauses"][cl["clause_no"]] = clause_id
        for sc in cl["scenarios"]:
            sc_id = repo.upsert_scenario(clause_id, sc)
            mapping["scenarios"][sc["scenario_no"]] = {
                "scenario_id": sc_id, "clause_id": clause_id, "clause_no": cl["clause_no"],
                "scenario": sc,
            }
    # Device-level plan anchor (Phase 1 is device-wide, not clause-specific)
    pre_clause = repo.upsert_clause(
        device["id"],
        {"clause_no": "0", "title": "Preconditions and environment setup",
         "requirement_text": "Device-level preconditions required by every clause."},
        source,
    )
    mapping["precondition_clause_id"] = pre_clause
    mapping["ingested_at"] = utcnow()
    return mapping
