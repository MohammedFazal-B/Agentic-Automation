"""Phase 3: execution engine with bounded self-healing.

Loads a Phase-2 plan, executes it step by step under a Job ID, verifies a
deterministic state lock before any evidence is captured, and heals plan
defects in place (new immutable plan version) instead of aborting.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .evidence import EvidenceGuard, EvidenceRejected, StateLock
from .healing import Healer, apply_patch
from .planner.render import render_phase2
from .repo import Repo
from .tools.base import ToolErrorCode, ToolResult
from .tools.registry import ToolRegistry


@dataclass
class StepOutcome:
    ok: bool
    result: ToolResult = field(default_factory=lambda: ToolResult.failure("NONE", "not run"))
    screen_text: str = ""
    lock_ok: bool = False
    lock_detail: str = ""
    not_supported: bool = False
    evidence_id: int | None = None
    evidence_path: str = ""
    evidence_missing: str = ""


class Executor:
    def __init__(self, *, repo: Repo, registry: ToolRegistry, guard: EvidenceGuard,
                 healer: Healer, settings: Settings, out_dir: Path):
        self.repo = repo
        self.registry = registry
        self.guard = guard
        self.healer = healer
        self.settings = settings
        self.out_dir = Path(out_dir)

    # ------------------------------------------------------------------ api
    def execute_scenario(
        self,
        *,
        device: dict,
        clause: dict,
        scenario: dict,
        plan_row: dict,
        kind: str = "execute",
        script_id: int | None = None,
    ) -> dict:
        plan = json.loads(plan_row["content_json"])
        steps: list[dict] = plan["steps"]

        job_id = self.repo.create_job(
            device_id=device["id"], clause_id=clause["id"], scenario_id=scenario["id"],
            plan_id=plan_row["id"], script_id=script_id, kind=kind,
            meta={"scenario_no": scenario["scenario_no"], "plan_version": plan_row["version"]},
        )
        self.repo.set_job_state(job_id, "running")
        self.repo.log_action(job_id, kind="state_change", op="execute.start",
                             target=scenario["scenario_no"], outcome="ok",
                             detail={"plan_id": plan_row["id"],
                                     "plan_version": plan_row["version"]})

        trace: list[dict] = []
        texts: list[str] = []
        heals: list[dict] = []
        plan_version = plan_row["version"]
        not_supported_detail = ""
        evidence_missing = 0

        for step in steps:
            outcome, step_heals, plan_version, plan, steps = self._run_step_with_healing(
                job_id=job_id, device=device, clause=clause, scenario=scenario,
                plan=plan, step=step, plan_version=plan_version, plan_row=plan_row,
            )
            heals.extend(step_heals)
            texts.append(outcome.screen_text)
            if outcome.not_supported:
                not_supported_detail = outcome.result.message
            if outcome.evidence_missing:
                evidence_missing += 1
            trace.append({
                "step_no": step["step_no"],
                "step_id": step.get("step_id"),
                "intent": step.get("intent"),
                "expected": step.get("expected"),
                "tool": step.get("tool"),
                "op": step.get("op"),
                "args": step.get("args"),
                "lock": step.get("lock"),
                "evidence": step.get("evidence"),
                "ok": outcome.ok,
                "not_supported": outcome.not_supported,
                "evidence_id": outcome.evidence_id,
                "evidence_path": outcome.evidence_path,
                "evidence_missing": outcome.evidence_missing,
                "healed": len(step_heals),
                "error": "" if outcome.ok else outcome.result.message,
            })
            if outcome.not_supported:
                break
            if not outcome.ok:
                break

        failed = [t for t in trace if not t["ok"] and not t["not_supported"]]
        if not_supported_detail:
            result, verdict = "not_supported", "not_supported"
            summary = f"Not supported on firmware {device.get('firmware')}: {not_supported_detail}"
        elif failed:
            result, verdict = "fail", "fail"
            summary = f"Step {failed[0]['step_no']} failed: {failed[0]['error']}"
        else:
            crit = self._evaluate(plan.get("pass_criteria", []), texts)
            met = all(c["met"] for c in crit)
            result = "pass" if met else "fail"
            verdict = "pass" if met else "fail"
            summary = (f"{len(crit)} pass criteria evaluated, "
                       f"{sum(1 for c in crit if c['met'])} met"
                       + (f"; {evidence_missing} step(s) without evidence" if evidence_missing else ""))
            plan["criteria"] = crit
            if met and evidence_missing:
                result = "partial"
                verdict = "partial"

        evidence_rows = self.repo.evidence_for_job(job_id)
        self.repo.add_verdict(
            job_id=job_id, device_id=device["id"], clause_no=clause["clause_no"],
            scenario_no=scenario["scenario_no"], result=result,
            criteria_met=plan.get("criteria", []),
            evidence_ids=[e["id"] for e in evidence_rows],
            notes=summary,
        )

        # Phase 3: persist the *actual* executed path (post-healing) as its own plan.
        trace_md = self._render_trace(device, clause, scenario, trace, heals, job_id, result)
        trace_path = Path(self.out_dir) / f"phase3_trace_{device['model']}_{scenario['scenario_no']}.md".replace(" ", "")
        trace_path.write_text(trace_md, encoding="utf-8")
        self.repo.save_plan(
            device_id=device["id"], clause_id=clause["id"], scenario_id=scenario["id"],
            phase=3, content_md=trace_md,
            content_json={"schema": "itsar.trace.v1", "job_id": job_id, "steps": trace,
                          "heals": heals, "result": result, "criteria": plan.get("criteria", [])},
            path_md=str(trace_path), parent_plan_id=plan_row["id"], status="approved",
        )

        self.repo.set_job_state(job_id, "passed" if result in ("pass",) else
                                ("not_supported" if result == "not_supported" else "failed"),
                                verdict=verdict, summary=summary)
        return {"job_id": job_id, "result": result, "verdict": verdict, "summary": summary,
                "trace": trace, "heals": heals, "plan": plan, "plan_version": plan_version,
                "evidence": evidence_rows, "criteria": plan.get("criteria", []),
                "trace_path": str(trace_path)}

    # ------------------------------------------------------------- one step
    def _run_step_with_healing(self, *, job_id, device, clause, scenario, plan, step,
                               plan_version, plan_row):
        heals: list[dict] = []
        attempt = 0
        while True:
            outcome = self._attempt(job_id, step)
            if outcome.ok:
                return outcome, heals, plan_version, plan, plan["steps"]
            if outcome.not_supported:
                return outcome, heals, plan_version, plan, plan["steps"]

            if attempt >= self.settings.max_heal_attempts:
                self.repo.log_action(job_id, kind="state_change", op="heal.exhausted",
                                     target=f"step{step['step_no']}", outcome="error",
                                     detail={"attempts": attempt})
                return outcome, heals, plan_version, plan, plan["steps"]

            attempt += 1
            self.repo.set_job_state(job_id, "healing")
            proposal = self.healer.diagnose(
                job_id=job_id, device=device, clause=clause, scenario=scenario,
                step=step, result=outcome.result, attempt=attempt,
            )
            if proposal.patch.get("kind") == "abort" or proposal.approval_required:
                self.repo.add_heal(
                    job_id=job_id, attempt=attempt, diagnosis=proposal.diagnosis,
                    strategy=proposal.strategy, patch=proposal.to_dict(),
                    plan_version_before=plan_version, plan_version_after=None,
                    applied=False, approval_required=proposal.approval_required,
                    outcome="escalated" if proposal.approval_required else "aborted",
                )
                self.repo.log_action(job_id, kind="state_change", op="heal.escalated",
                                     target=f"step{step['step_no']}", outcome="error",
                                     detail={"diagnosis": proposal.diagnosis})
                return outcome, heals, plan_version, plan, plan["steps"]

            if proposal.patch.get("kind") == "mark_not_supported":
                step["_not_supported"] = proposal.patch["changes"].get("detail", "")
                self.repo.add_heal(
                    job_id=job_id, attempt=attempt, diagnosis=proposal.diagnosis,
                    strategy=proposal.strategy, patch=proposal.to_dict(),
                    plan_version_before=plan_version, plan_version_after=None,
                    applied=True, outcome="not_supported",
                )
                heals.append({"attempt": attempt, "strategy": proposal.strategy,
                              "diagnosis": proposal.diagnosis, "applied": True})
                outcome2 = self._attempt(job_id, step)
                outcome2.not_supported = True
                return outcome2, heals, plan_version, plan, plan["steps"]

            # ---- apply + persist as a NEW immutable plan version
            new_plan = apply_patch(plan, step["step_no"], proposal.patch, self.registry)
            new_version = self.repo.next_plan_version(
                device["id"], 2, clause["id"], scenario["id"])
            md = render_phase2(device, clause,
                               {**scenario,
                                "mutation_scope": json.loads(scenario.get("mutation_scope_json") or "[]")},
                               new_plan["steps"], new_plan.get("pass_criteria", []),
                               new_plan.get("fail_criteria", []),
                               [f"HEALED (attempt {attempt}, {proposal.strategy}): "
                                f"{proposal.diagnosis}"],
                               job_id)
            self.repo.save_plan(
                device_id=device["id"], clause_id=clause["id"], scenario_id=scenario["id"],
                phase=2, content_md=md, content_json=new_plan,
                path_md=str(Path(self.out_dir) /
                            f"phase2_{device['model']}_{scenario['scenario_no']}_v{new_version}.md".replace(" ", "")),
                parent_plan_id=plan_row["id"], created_by="agent:heal", status="executing",
            )
            self.repo.add_heal(
                job_id=job_id, attempt=attempt, diagnosis=proposal.diagnosis,
                strategy=proposal.strategy, patch=proposal.to_dict(),
                plan_version_before=plan_version, plan_version_after=new_version,
                applied=True, outcome="retrying",
            )
            self.repo.log_action(job_id, kind="state_change", op="plan.healed",
                                 target=f"step{step['step_no']}", outcome="ok",
                                 detail={"strategy": proposal.strategy,
                                         "plan_version_before": plan_version,
                                         "plan_version_after": new_version,
                                         "patch": proposal.patch})
            heals.append({"attempt": attempt, "strategy": proposal.strategy,
                          "diagnosis": proposal.diagnosis, "applied": True})
            plan, plan_version = new_plan, new_version
            step = next(s for s in plan["steps"] if s["step_no"] == step["step_no"])
            self.repo.set_job_state(job_id, "running")

    # -------------------------------------------------------------- attempt
    def _attempt(self, job_id: str, step: dict) -> StepOutcome:
        if step.get("_not_supported"):
            return StepOutcome(ok=False, not_supported=True,
                               result=ToolResult.failure(ToolErrorCode.UNSUPPORTED_FEATURE,
                                                         step["_not_supported"]))

        if step.get("_remediate"):
            ad = self.registry.get("cli")
            if ad is not None:
                rem = ad.run(step["_remediate"])
                self.repo.log_action(job_id, kind="tool_call", tool="cli",
                                     op=step["_remediate"], target=f"step{step['step_no']}",
                                     outcome="ok" if rem.ok else "error",
                                     latency_ms=rem.latency_ms,
                                     detail={"remediation": True, "code": rem.code})

        combined_text = ""
        last: ToolResult = ToolResult.failure("NONE", "no op executed")
        for tool_name, op, args in self._expand(step):
            adapter = self.registry.get(tool_name)
            if adapter is None:
                return StepOutcome(ok=False, result=ToolResult.failure(
                    "UNKNOWN", f"tool '{tool_name}' is not registered"))
            res = adapter.run(op, **args)
            self.repo.log_action(
                job_id, kind="tool_call", tool=tool_name, op=op,
                target=f"step{step['step_no']}:{args.get('label') or args.get('url') or args.get('command', '')}"[:200],
                request=args, response=(res.text or res.message)[:2000],
                outcome="ok" if res.ok else "error", latency_ms=res.latency_ms,
                detail={"code": res.code, "meta": res.meta},
            )
            combined_text += ("\n" + res.text) if res.text else ""
            last = res
            if not res.ok:
                if res.code == ToolErrorCode.UNSUPPORTED_FEATURE:
                    return StepOutcome(ok=False, not_supported=True, result=res,
                                       screen_text=combined_text)
                if op == "click":
                    res.meta = {**(res.meta or {}), "label": args.get("label"),
                                "failed_click_index": (step.get("args", {})
                                                       .get("click_path") or []).index(args.get("label"))
                                if args.get("label") in (step.get("args", {}).get("click_path") or [])
                                else None}
                return StepOutcome(ok=False, result=res, screen_text=combined_text)

        lock = StateLock.from_dict(step.get("lock"))
        lock_ok, lock_detail = lock.verify(combined_text, last)
        if not lock_ok:
            self.repo.log_action(job_id, kind="lock_fail", op="state_lock",
                                 target=f"step{step['step_no']}", outcome="error",
                                 detail={"lock": step.get("lock"), "detail": lock_detail})
            return StepOutcome(
                ok=False, screen_text=combined_text,
                result=ToolResult.failure("LOCK_NOT_VERIFIED", lock_detail, text=combined_text),
            )
        self.repo.log_action(job_id, kind="lock_ok", op="state_lock",
                             target=f"step{step['step_no']}", outcome="ok",
                             detail={"lock": step.get("lock"), "detail": lock_detail})

        outcome = StepOutcome(ok=True, result=last, screen_text=combined_text,
                              lock_ok=True, lock_detail=lock_detail)
        if step.get("evidence"):
            adapter = self.registry.get(step["tool"]) or self.registry.get("browser")
            kind = "terminal" if (adapter is not None and adapter.kind == "cli") else "screen"
            try:
                ev = self.guard.capture(
                    job_id=job_id, step_id=step.get("step_id"), step_no=step["step_no"],
                    adapter=adapter, lock=lock, lock_ok=True, lock_detail=lock_detail,
                    screen_text=combined_text,
                    title=f"Step {step['step_no']} — {step.get('intent', '')[:90]}", kind=kind,
                )
                outcome.evidence_id = ev["id"]
                outcome.evidence_path = ev["path"]
            except EvidenceRejected as exc:
                outcome.evidence_missing = f"{exc.reason}: {exc.detail}"
                self.repo.log_action(job_id, kind="lock_fail", op="evidence.refused",
                                     target=f"step{step['step_no']}", outcome="refused",
                                     detail={"reason": exc.reason, "detail": exc.detail})
        return outcome

    def _expand(self, step: dict) -> list[tuple[str, str, dict]]:
        tool, op = step["tool"], step["op"]
        args = dict(step.get("args") or {})
        if tool == "browser":
            if op == "navigate_path":
                ops = [("browser", "goto", {"url": args.get("start_url", "/dashboard")})]
                ops += [("browser", "click", {"label": lb}) for lb in args.get("click_path", [])]
                return ops
            if op == "verify_text":
                ops = []
                if args.get("click_path"):
                    ops.append(("browser", "goto", {"url": args.get("start_url", "/dashboard")}))
                    ops += [("browser", "click", {"label": lb}) for lb in args["click_path"]]
                ops.append(("browser", "read_text", {}))
                return ops
            return [("browser", op, args)]
        if op == "show":
            return [(tool, "exec", {"command": args.get("command", "")})]
        if op == "config":
            return [(tool, "config", {"command": args.get("command", "")})]
        return [(tool, op, args)]

    # ------------------------------------------------------------ criteria
    def _evaluate(self, criteria: list[str], texts: list[str]) -> list[dict]:
        blob = "\n".join(texts)
        out = []
        for c in criteria:
            numbers = re.findall(r"\b\d{1,3}\b", str(c))
            words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", str(c))
                     if w.lower() not in _STOP]
            hits = [w for w in words if w in blob.lower()]
            nums_ok = all(n in blob for n in numbers) if numbers else True
            met = nums_ok and (len(hits) >= max(2, int(len(set(words)) * 0.4)))
            out.append({"criterion": c, "met": bool(met),
                        "matched_terms": sorted(set(hits))[:8],
                        "missing_numbers": [n for n in numbers if n not in blob]})
        return out

    # --------------------------------------------------------------- trace
    def _render_trace(self, device, clause, scenario, trace, heals, job_id, result) -> str:
        rows = []
        for t in trace:
            rows.append([
                t["step_no"], (t["intent"] or "")[:90], f"`{t['tool']}/{t['op']}`",
                "✅" if t["ok"] else ("N/S" if t["not_supported"] else "❌"),
                f"`{t['evidence_path']}`" if t["evidence_path"] else
                (t["evidence_missing"] or "—"),
                t["healed"],
            ])
        md = [f"# Phase 3 — Executed Trace",
              "",
              f"**Device:** {device.get('model')} ({device.get('firmware')})  |  "
              f"**Scenario:** {scenario.get('scenario_no')}  |  **Job:** `{job_id}`  ",
              f"**Result:** **{result.upper()}**", "",
              "| # | Step | Tool/op | Outcome | Evidence | Heals |",
              "|---|---|---|---|---|---|"]
        md += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        if heals:
            md += ["", "## Self-healing events", ""]
            for h in heals:
                md += [f"- **attempt {h['attempt']}** `{h['strategy']}` — {h['diagnosis']}"]
        return "\n".join(md)


_STOP = {
    "the", "and", "that", "with", "must", "should", "shall", "have", "been", "this",
    "from", "into", "when", "then", "than", "only", "also", "upon", "does", "will",
    "least", "value", "config", "configured", "setting", "settings", "enabled",
}
