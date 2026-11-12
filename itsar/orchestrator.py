"""Pipeline orchestration: the five phases, end to end.

This module is the only place that knows the order of operations. Each phase
reads from and writes to the database, so a run can be resumed or audited at
any phase boundary.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import report as report_mod
from .config import Settings
from .evidence import EvidenceGuard
from .executor import Executor
from .healing import Healer
from .llm import LlmDiagnosis
from .parsing.report_parser import ApprovedReport, ingest, parse_report
from .planner import phase1 as phase1_mod
from .planner import phase2 as phase2_mod
from .repo import Repo, utcnow
from .scripts import gen
from .scripts import reuse as reuse_mod
from .tools.registry import ToolRegistry


def load_credentials(settings: Settings) -> dict:
    """Credentials live outside the database and outside the plans."""
    p = Path(settings.credential_file)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


class Pipeline:
    def __init__(self, settings: Settings, repo: Repo, registry: ToolRegistry):
        self.settings = settings
        self.repo = repo
        self.registry = registry
        self.guard = EvidenceGuard(repo, settings.evidence_dir)
        llm = LlmDiagnosis(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        self.healer = Healer(repo, registry, llm=llm, max_attempts=settings.max_heal_attempts)
        self.executor = Executor(repo=repo, registry=registry, guard=self.guard,
                                 healer=self.healer, settings=settings,
                                 out_dir=settings.out_dir)
        self.settings.ensure_dirs()

    # ------------------------------------------------------------- ingest
    def ingest(self, source: str | Path | dict, device_spec: dict) -> dict:
        rep: ApprovedReport = parse_report(source)
        self.report_obj = rep
        mapping = ingest(self.repo, rep, device_spec)
        mapping["preconditions"] = rep.preconditions
        mapping["objectives"] = rep.objectives
        mapping["report_meta"] = rep.meta
        mapping["report"] = rep
        return mapping

    # ------------------------------------------------------------ phase 1
    def run_phase1(self, ctx: dict) -> dict:
        device = dict(ctx["device"])
        device["_clause_id"] = ctx["precondition_clause_id"]
        objectives = ctx.get("objectives") or [
            "Confirm every protocol, port and interface required by the approved report is "
            "reachable on this DUT.",
            "Automatically remediate any disabled but required management service.",
            "Record any capability that this firmware does not implement as Not Supported.",
            "Produce a human-reviewable preconditions plan before any test traffic is generated.",
        ]
        return phase1_mod.execute_phase1(
            repo=self.repo, registry=self.registry, device=device,
            preconditions=ctx.get("preconditions") or {}, objectives=objectives,
            guard=self.guard, out_dir=self.settings.out_dir,
        )

    # ------------------------------------------------------------ phase 2
    def run_phase2(self, ctx: dict) -> list[dict]:
        device = dict(ctx["device"])
        out = []
        for clause, sc in self._iter_scenarios(ctx):
            steps = self.repo.steps_for(self.repo.scenario_by_no(device["id"], sc["scenario_no"])["id"])
            res = phase2_mod.execute_phase2(
                repo=self.repo, registry=self.registry, device=device, clause=clause,
                scenario=sc, steps=steps, out_dir=self.settings.out_dir,
            )
            out.append(res)
        return out

    # ------------------------------------------------------------ phase 3
    def run_phase3(self, ctx: dict) -> list[dict]:
        device = dict(ctx["device"])
        results = []
        for clause, sc in self._iter_scenarios(ctx):
            sc_row = self.repo.scenario_by_no(device["id"], sc["scenario_no"])
            plan = self.repo.latest_plan(device["id"], 2, clause["id"], sc_row["id"])
            if plan is None:
                raise RuntimeError(f"no Phase-2 plan for scenario {sc['scenario_no']}")
            res = self.executor.execute_scenario(device=device, clause=clause,
                                                 scenario=sc_row, plan_row=plan)
            res["scenario_no"] = sc["scenario_no"]
            res["clause_no"] = clause["clause_no"]
            if res["result"] == "pass":
                res["script_id"] = self._emit_v1(device, clause, sc_row, res)
            results.append(res)
        return results

    def _emit_v1(self, device: dict, clause: dict, scenario_row: dict, res: dict) -> int:
        scenario_view = dict(scenario_row)
        content, params, _meta = gen.generate(
            device=device, clause=clause, scenario=scenario_view,
            trace=res["trace"], pass_criteria=res["plan"].get("pass_criteria", []),
            job_id=res["job_id"], version=1, generated=utcnow(),
        )
        path = Path(self.settings.out_dir) / \
            f"scenario_{scenario_row['scenario_no']}_v1_{device['model']}.py".replace(" ", "")
        path.write_text(content, encoding="utf-8")
        return self.repo.add_script(
            device_id=device["id"], scenario_id=scenario_row["id"], content=content,
            params=params, file_path=str(path), source_job_id=res["job_id"],
            source_trace={"trace": res["trace"]}, version=1,
        )

    # ------------------------------------------------------------ phase 4
    def run_phase4(self, ctx: dict) -> list[dict]:
        """Reuse plans/scripts from a sibling device for this (new) device."""
        device = dict(ctx["device"])
        results = []
        for clause, sc in self._iter_scenarios(ctx):
            sc_row = self.repo.scenario_by_no(device["id"], sc["scenario_no"])
            source = self.repo.sibling_script_for_scenario_no(device["id"], sc["scenario_no"])
            sibling_plan = self.repo.sibling_plan_for_scenario_no(device["id"], sc["scenario_no"], 2)

            if sibling_plan is None:
                results.append({"scenario_no": sc["scenario_no"], "mode": "no_history",
                                "result": "skipped", "note": "no sibling plan to reuse"})
                continue

            adapted_plan_id = self.repo.save_plan(
                device_id=device["id"], clause_id=clause["id"], scenario_id=sc_row["id"],
                phase=2, content_md=sibling_plan["content_md"],
                content_json=json.loads(sibling_plan["content_json"]),
                path_md=sibling_plan["path_md"], adapted_from=sibling_plan["id"],
                created_by="agent:reuse", status="approved",
            )
            adapted = self.repo.latest_plan(device["id"], 2, clause["id"], sc_row["id"])
            entry = {"scenario_no": sc["scenario_no"], "adapted_plan_id": adapted_plan_id,
                     "source_plan_id": sibling_plan["id"], "clause_no": clause["clause_no"]}

            if source is None:
                res = self.executor.execute_scenario(device=device, clause=clause,
                                                     scenario=sc_row, plan_row=adapted)
                entry.update(mode="plan_only", result=res["result"], job_id=res["job_id"],
                             heals=len(res["heals"]))
                if res["result"] == "pass":
                    entry["script_id"] = self._emit_v1(device, clause, sc_row, res)
                results.append(entry)
                continue

            entry["source_script_id"] = source["id"]
            entry["source_device"] = f"{source['src_model']} ({source['src_firmware']})"
            run = reuse_mod.run_rebound_script(repo=self.repo, device=device,
                                               scenario_row=sc_row, source_script=source)
            entry["reuse_job_id"] = run["job_id"]

            if run["result"].ok:
                # clean rebind -> exactly this script becomes the new DUT's v1
                sid = reuse_mod.save_v1_for_new_device(
                    repo=self.repo, device=device, scenario_row=sc_row, source_script=source,
                    rebound_params=run["params"], job_id=run["job_id"],
                    out_dir=self.settings.out_dir,
                )
                self.repo.add_verdict(
                    job_id=run["job_id"], device_id=device["id"], clause_no=clause["clause_no"],
                    scenario_no=sc["scenario_no"], result="pass",
                    criteria_met=run["result"].criteria, evidence_ids=[],
                    notes="v1 script from sibling device ran unchanged after credential/host rebind",
                )
                self.repo.set_job_state(run["job_id"], "passed", verdict="pass",
                                        summary="clean reuse: rebind only")
                entry.update(mode="rebind_only", result="pass", script_id=sid, version=1,
                             param_diff=reuse_mod.param_diff(
                                 json.loads(source["params_json"]), run["params"]))
            else:
                # rebind was not enough -> heal with the executor, then store v2
                self.repo.set_job_state(run["job_id"], "blocked", verdict="fail",
                                        summary=f"v1 failed at step {run['result'].failed_step}: "
                                                f"{run['result'].code}")
                self.repo.add_verdict(
                    job_id=run["job_id"], device_id=device["id"], clause_no=clause["clause_no"],
                    scenario_no=sc["scenario_no"], result="fail", criteria_met=[],
                    evidence_ids=[],
                    notes=f"sibling v{source['version']} script failed on this firmware: "
                          f"{run['result'].code} {run['result'].message}"[:400],
                )
                exec_res = self.executor.execute_scenario(
                    device=device, clause=clause, scenario=sc_row, plan_row=adapted,
                    kind="execute",
                )
                entry.update(mode="healed", result=exec_res["result"],
                             job_id=exec_res["job_id"], heals=len(exec_res["heals"]),
                             v1_failure={"step": run["result"].failed_step,
                                         "code": run["result"].code,
                                         "message": run["result"].message})
                if exec_res["result"] == "pass":
                    entry["script_id"] = reuse_mod.save_v2_for_new_device(
                        repo=self.repo, device=device, clause=clause, scenario_row=sc_row,
                        source_script=source, exec_result=exec_res,
                        out_dir=self.settings.out_dir,
                    )
                    entry["version"] = 2
            results.append(entry)
        return results

    # ------------------------------------------------------------ phase 5
    def run_phase5(self, ctx: dict) -> dict:
        device = self.repo.get_device(ctx["device"]["id"])
        fam = self.repo.db.query_one("SELECT name FROM device_family WHERE id=?",
                                     (device["family_id"],))
        return report_mod.build(self.repo, device, ctx.get("report_meta", {}),
                                fam["name"] if fam else "", self.settings.out_dir)

    # ------------------------------------------------------------ helpers
    def _iter_scenarios(self, ctx: dict):
        device = ctx["device"]
        for clause_no, clause_id in ctx["clauses"].items():
            clause = self.repo.db.query_one("SELECT * FROM clause WHERE id=?", (clause_id,))
            for sc_no in [k for k in ctx["scenarios"]
                          if ctx["scenarios"][k]["clause_id"] == clause_id]:
                sc = self.repo.scenario_by_no(device["id"], sc_no)
                if sc:
                    yield clause, sc

    # ------------------------------------------------------------- full run
    def run_all(self, source, device_spec: dict) -> dict:
        ctx = self.ingest(source, device_spec)
        ctx["device"]["_profile_name"] = device_spec.get("_profile_name", "old")
        p1 = self.run_phase1(ctx)
        p2 = self.run_phase2(ctx)
        p3 = self.run_phase3(ctx)
        rep = self.run_phase5(ctx)
        return {"device": ctx["device"], "phase1": p1, "phase2": p2, "phase3": p3,
                "report": rep, "ctx": ctx}
