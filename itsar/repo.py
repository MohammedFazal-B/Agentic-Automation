"""Repository layer: the only place that talks SQL.

Every write that the spec says "must be stored in PostgreSQL" goes through
here, and every method takes explicit device/clause/scenario/step identity so
nothing can be written without provenance.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from .db import Database


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_job_id(device: dict, clause_no: str | None, scenario_no: str | None, kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tail = secrets.token_hex(3).upper()
    model = (device.get("model") or "dev").replace(" ", "")
    parts = ["JOB", model, clause_no or "C00", scenario_no or "S00", kind.upper(), stamp, tail]
    return "-".join(parts)


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- devices
    def ensure_family(self, vendor: str, name: str, base_platform: str = "", notes: str = "") -> int:
        row = self.db.query_one(
            "SELECT id FROM device_family WHERE vendor=? AND name=?", (vendor, name)
        )
        if row:
            return int(row["id"])
        return self.db.insert(
            "device_family",
            {
                "vendor": vendor,
                "name": name,
                "base_platform": base_platform,
                "notes": notes,
                "created_at": utcnow(),
            },
        )

    def ensure_device(self, spec: dict) -> dict:
        """spec: family{vendor,name,base_platform}, model, firmware, host, protocol,
        credential_ref, serial, meta."""
        fam = spec.get("family") or {}
        family_id = self.ensure_family(
            fam.get("vendor", "unknown"),
            fam.get("name", "unknown"),
            fam.get("base_platform", ""),
            fam.get("notes", ""),
        )
        existing = self.db.query_one(
            "SELECT * FROM device WHERE model=? AND firmware=? AND host=?",
            (spec["model"], spec["firmware"], spec["host"]),
        )
        if existing:
            return existing
        dev_id = self.db.insert(
            "device",
            {
                "family_id": family_id,
                "model": spec["model"],
                "firmware": spec["firmware"],
                "host": spec["host"],
                "mgmt_protocol": spec.get("protocol", "https"),
                "credential_ref": spec["credential_ref"],
                "serial": spec.get("serial", ""),
                "status": "active",
                "meta_json": jdump(spec.get("meta", {})),
                "created_at": utcnow(),
            },
        )
        return self.get_device(dev_id)

    def get_device(self, device_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM device WHERE id=?", (device_id,))
        if not row:
            raise KeyError(f"device {device_id} not found")
        return row

    def device_family_members(self, family_id: int) -> list[dict]:
        return self.db.query("SELECT * FROM device WHERE family_id=? ORDER BY id", (family_id,))

    # -------------------------------------------------- clause/scenario/step
    def upsert_clause(self, device_id: int, clause: dict, source_report: str) -> int:
        row = self.db.query_one(
            "SELECT id FROM clause WHERE device_id=? AND clause_no=?",
            (device_id, clause["clause_no"]),
        )
        if row:
            return int(row["id"])
        return self.db.insert(
            "clause",
            {
                "device_id": device_id,
                "clause_no": clause["clause_no"],
                "title": clause.get("title", ""),
                "requirement_text": clause.get("requirement_text", ""),
                "source_report": source_report,
                "created_at": utcnow(),
            },
        )

    def upsert_scenario(self, clause_id: int, sc: dict) -> int:
        row = self.db.query_one(
            "SELECT id FROM scenario WHERE clause_id=? AND scenario_no=?",
            (clause_id, sc["scenario_no"]),
        )
        if row:
            return int(row["id"])
        sc_id = self.db.insert(
            "scenario",
            {
                "clause_id": clause_id,
                "scenario_no": sc["scenario_no"],
                "title": sc.get("title", ""),
                "intent": sc.get("intent", ""),
                "preconditions_json": jdump(sc.get("preconditions", [])),
                "pass_criteria_json": jdump(sc.get("pass_criteria", [])),
                "fail_criteria_json": jdump(sc.get("fail_criteria", [])),
                "mutation_scope_json": jdump(sc.get("mutation_scope", [])),
                "severity": sc.get("severity", ""),
                "created_at": utcnow(),
            },
        )
        for st in sc.get("steps", []):
            self.db.insert(
                "step",
                {
                    "scenario_id": sc_id,
                    "step_no": st["step_no"],
                    "action": st["action"],
                    "expected": st.get("expected", ""),
                    "tool_hint": st.get("tool_hint", ""),
                    "locator_hint": st.get("locator_hint", ""),
                },
            )
        return sc_id

    def steps_for(self, scenario_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM step WHERE scenario_id=? ORDER BY step_no", (scenario_id,)
        )

    def scenario_context(self, scenario_id: int) -> dict:
        sc = self.db.query_one("SELECT * FROM scenario WHERE id=?", (scenario_id,))
        if not sc:
            raise KeyError(f"scenario {scenario_id} not found")
        cl = self.db.query_one("SELECT * FROM clause WHERE id=?", (sc["clause_id"],))
        return {"scenario": sc, "clause": cl, "steps": self.steps_for(scenario_id)}

    # --------------------------------------------------------------- plans
    def next_plan_version(self, device_id: int, phase: int, clause_id: int, scenario_id: int) -> int:
        cur = self.db.scalar(
            "SELECT MAX(version) FROM plan WHERE device_id=? AND phase=? AND clause_id=? AND scenario_id=?",
            (device_id, phase, clause_id, scenario_id),
            default=0,
        )
        return int(cur or 0) + 1

    def save_plan(
        self,
        *,
        device_id: int,
        clause_id: int,
        phase: int,
        content_md: str,
        content_json: dict,
        scenario_id: int = 0,
        path_md: str | None = None,
        parent_plan_id: int | None = None,
        adapted_from: int | None = None,
        created_by: str = "agent",
        status: str = "approved",
    ) -> int:
        version = self.next_plan_version(device_id, phase, clause_id, scenario_id)
        return self.db.insert(
            "plan",
            {
                "device_id": device_id,
                "clause_id": clause_id,
                "scenario_id": scenario_id,
                "phase": phase,
                "version": version,
                "path_md": path_md or "",
                "content_md": content_md,
                "content_json": jdump(content_json),
                "content_hash": sha256_text(content_md + jdump(content_json)),
                "parent_plan_id": parent_plan_id,
                "adapted_from": adapted_from,
                "status": status,
                "created_by": created_by,
                "created_at": utcnow(),
            },
        )

    def latest_plan(self, device_id: int, phase: int, clause_id: int, scenario_id: int = 0) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM plan WHERE device_id=? AND phase=? AND clause_id=? AND scenario_id=? "
            "ORDER BY version DESC LIMIT 1",
            (device_id, phase, clause_id, scenario_id),
        )

    def plans_for_device(self, device_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM plan WHERE device_id=? ORDER BY phase, clause_id, scenario_id, version",
            (device_id,),
        )

    def plans_of_family_siblings(self, device_id: int, phase: int, clause_id: int, scenario_id: int) -> list[dict]:
        """Historical plans from OTHER devices in the same family (Phase 4 reuse)."""
        dev = self.get_device(device_id)
        return self.db.query(
            "SELECT p.* FROM plan p JOIN device d ON d.id = p.device_id "
            "WHERE d.family_id=? AND d.id<>? AND p.phase=? AND p.clause_id=? AND p.scenario_id=? "
            "ORDER BY p.version DESC",
            (dev["family_id"], device_id, phase, clause_id, scenario_id),
        )

    # ---------------------------------------------------------------- jobs
    def create_job(
        self,
        *,
        device_id: int,
        kind: str,
        clause_id: int | None = None,
        scenario_id: int | None = None,
        plan_id: int | None = None,
        script_id: int | None = None,
        meta: dict | None = None,
    ) -> str:
        dev = self.get_device(device_id)
        cl = self.db.query_one("SELECT clause_no FROM clause WHERE id=?", (clause_id,)) if clause_id else None
        sc = self.db.query_one("SELECT scenario_no FROM scenario WHERE id=?", (scenario_id,)) if scenario_id else None
        job_id = new_job_id(dev, cl and cl["clause_no"], sc and sc["scenario_no"], kind)
        self.db.insert(
            "job",
            {
                "id": job_id,
                "device_id": device_id,
                "clause_id": clause_id,
                "scenario_id": scenario_id,
                "plan_id": plan_id,
                "script_id": script_id,
                "kind": kind,
                "state": "created",
                "started_at": utcnow(),
                "meta_json": jdump(meta or {}),
            },
        )
        return job_id

    def set_job_state(self, job_id: str, state: str, verdict: str | None = None, summary: str | None = None) -> None:
        fields = ["state=?"]
        params: list[Any] = [state]
        if verdict is not None:
            fields.append("verdict=?")
            params.append(verdict)
        if summary is not None:
            fields.append("summary=?")
            params.append(summary)
        if state in ("passed", "failed", "blocked", "not_supported", "aborted"):
            fields.append("ended_at=?")
            params.append(utcnow())
        params.append(job_id)
        self.db.execute(f"UPDATE job SET {', '.join(fields)} WHERE id=?", params)

    def get_job(self, job_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM job WHERE id=?", (job_id,))
        if not row:
            raise KeyError(job_id)
        return row

    # -------------------------------------------------------- action log
    def log_action(
        self,
        job_id: str,
        *,
        kind: str,
        tool: str = "",
        op: str = "",
        target: str = "",
        request: Any = None,
        response: Any = None,
        outcome: str = "ok",
        latency_ms: int = 0,
        detail: dict | None = None,
    ) -> int:
        seq = int(self.db.scalar("SELECT COALESCE(MAX(seq),0) FROM job_action WHERE job_id=?", (job_id,), 0)) + 1
        return self.db.insert(
            "job_action",
            {
                "job_id": job_id,
                "seq": seq,
                "ts": utcnow(),
                "kind": kind,
                "tool": tool,
                "op": op,
                "target": target,
                "request_digest": sha256_text(jdump(request))[:16] if request is not None else "",
                "response_digest": sha256_text(jdump(response))[:16] if response is not None else "",
                "outcome": outcome,
                "latency_ms": latency_ms,
                "detail_json": jdump(detail or {}),
            },
        )

    def actions_for(self, job_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM job_action WHERE job_id=? ORDER BY seq", (job_id,))

    # ---------------------------------------------------------- evidence
    def add_evidence(
        self,
        *,
        job_id: str,
        step_id: int | None,
        file_path: str,
        sha256: str,
        lock_kind: str,
        lock_detail: str,
        width: int = 0,
        height: int = 0,
    ) -> int:
        return self.db.insert(
            "evidence",
            {
                "job_id": job_id,
                "step_id": step_id,
                "file_path": file_path,
                "sha256": sha256,
                "lock_kind": lock_kind,
                "lock_detail": lock_detail,
                "width": width,
                "height": height,
                "captured_at": utcnow(),
                "verified": 1,
            },
        )

    def reject_evidence(self, *, job_id: str, step_id: int | None, reason: str, detail: str) -> int:
        return self.db.insert(
            "evidence_rejection",
            {
                "job_id": job_id,
                "step_id": step_id,
                "reason": reason,
                "detail": detail,
                "ts": utcnow(),
            },
        )

    def evidence_for_job(self, job_id: str) -> list[dict]:
        return self.db.query(
            "SELECT e.*, s.step_no FROM evidence e LEFT JOIN step s ON s.id=e.step_id "
            "WHERE e.job_id=? ORDER BY s.step_no",
            (job_id,),
        )

    def rejections_for_job(self, job_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM evidence_rejection WHERE job_id=? ORDER BY id", (job_id,))

    # ------------------------------------------------------------ healing
    def add_heal(
        self,
        *,
        job_id: str,
        attempt: int,
        diagnosis: str,
        strategy: str,
        patch: dict,
        plan_version_before: int | None,
        plan_version_after: int | None,
        applied: bool,
        approval_required: bool = False,
        outcome: str = "",
        trigger_action_id: int | None = None,
    ) -> int:
        return self.db.insert(
            "heal_event",
            {
                "job_id": job_id,
                "attempt": attempt,
                "trigger_action_id": trigger_action_id,
                "diagnosis": diagnosis,
                "strategy": strategy,
                "patch_json": jdump(patch),
                "plan_version_before": plan_version_before,
                "plan_version_after": plan_version_after,
                "applied": 1 if applied else 0,
                "approval_required": 1 if approval_required else 0,
                "outcome": outcome,
                "ts": utcnow(),
            },
        )

    def heals_for_job(self, job_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM heal_event WHERE job_id=? ORDER BY attempt", (job_id,))

    # ------------------------------------------------------------ scripts
    def add_script(
        self,
        *,
        device_id: int,
        scenario_id: int,
        content: str,
        params: dict,
        file_path: str = "",
        parent_script_id: int | None = None,
        source_job_id: str = "",
        source_trace: dict | None = None,
        language: str = "python",
        version: int | None = None,
    ) -> int:
        if version is None:
            version = int(
                self.db.scalar(
                    "SELECT MAX(version) FROM script WHERE device_id=? AND scenario_id=?",
                    (device_id, scenario_id),
                    default=0,
                )
                or 0
            ) + 1
        return self.db.insert(
            "script",
            {
                "device_id": device_id,
                "scenario_id": scenario_id,
                "version": version,
                "language": language,
                "file_path": file_path,
                "content": content,
                "content_hash": sha256_text(content),
                "params_json": jdump(params),
                "parent_script_id": parent_script_id,
                "source_job_id": source_job_id,
                "source_trace_json": jdump(source_trace or {}),
                "created_at": utcnow(),
            },
        )

    def latest_script(self, device_id: int, scenario_id: int) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM script WHERE device_id=? AND scenario_id=? ORDER BY version DESC LIMIT 1",
            (device_id, scenario_id),
        )

    def sibling_script_for_scenario_no(self, device_id: int, scenario_no: str) -> dict | None:
        """Latest script for a given scenario number, produced on ANOTHER device
        of the same family. This is the Phase-4 reuse input."""
        dev = self.get_device(device_id)
        return self.db.query_one(
            "SELECT s.*, d.model AS src_model, d.firmware AS src_firmware, sc.scenario_no "
            "FROM script s JOIN device d ON d.id=s.device_id "
            "JOIN scenario sc ON sc.id=s.scenario_id "
            "WHERE d.family_id=? AND d.id<>? AND sc.scenario_no=? "
            "ORDER BY s.version DESC, s.id DESC LIMIT 1",
            (dev["family_id"], device_id, scenario_no),
        )

    def sibling_plan_for_scenario_no(self, device_id: int, scenario_no: str,
                                     phase: int = 2) -> dict | None:
        dev = self.get_device(device_id)
        return self.db.query_one(
            "SELECT p.* FROM plan p JOIN scenario sc ON sc.id=p.scenario_id "
            "JOIN device d ON d.id=p.device_id "
            "WHERE d.family_id=? AND d.id<>? AND sc.scenario_no=? AND p.phase=? "
            "ORDER BY p.version DESC LIMIT 1",
            (dev["family_id"], device_id, scenario_no, phase),
        )

    def scenario_by_no(self, device_id: int, scenario_no: str) -> dict | None:
        return self.db.query_one(
            "SELECT sc.* FROM scenario sc JOIN clause c ON c.id=sc.clause_id "
            "WHERE c.device_id=? AND sc.scenario_no=?", (device_id, scenario_no),
        )

    def scripts_of_family_siblings(self, device_id: int, scenario_id: int) -> list[dict]:
        """v1 script(s) from an older device of the same family (Phase 4 reuse)."""
        dev = self.get_device(device_id)
        return self.db.query(
            "SELECT s.* FROM script s JOIN device d ON d.id=s.device_id "
            "WHERE d.family_id=? AND d.id<>? AND s.scenario_id=? ORDER BY s.version DESC",
            (dev["family_id"], device_id, scenario_id),
        )

    def add_script_run(
        self,
        *,
        script_id: int,
        device_id: int,
        job_id: str,
        outcome: str,
        heal_count: int = 0,
        diff: dict | None = None,
        started_at: str | None = None,
    ) -> int:
        return self.db.insert(
            "script_run",
            {
                "script_id": script_id,
                "device_id": device_id,
                "job_id": job_id,
                "outcome": outcome,
                "heal_count": heal_count,
                "diff_json": jdump(diff or {}),
                "started_at": started_at or utcnow(),
                "ended_at": utcnow(),
            },
        )

    def script_runs(self, device_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM script_run WHERE device_id=? ORDER BY id", (device_id,)
        )

    # ----------------------------------------------------------- verdicts
    def add_verdict(
        self,
        *,
        job_id: str,
        device_id: int,
        clause_no: str,
        scenario_no: str,
        result: str,
        criteria_met: dict | list,
        evidence_ids: list[int],
        notes: str = "",
    ) -> int:
        return self.db.insert(
            "verdict",
            {
                "job_id": job_id,
                "device_id": device_id,
                "clause_no": clause_no,
                "scenario_no": scenario_no,
                "result": result,
                "criteria_met_json": jdump(criteria_met),
                "evidence_ids_json": jdump(evidence_ids),
                "notes": notes,
                "created_at": utcnow(),
            },
        )

    def verdicts_for_device(self, device_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM verdict WHERE device_id=? ORDER BY clause_no, scenario_no", (device_id,)
        )
