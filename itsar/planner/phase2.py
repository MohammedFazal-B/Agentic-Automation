"""Phase 2: dry-run navigation discovery and execution-plan generation."""
from __future__ import annotations

import json
from pathlib import Path

from ..repo import Repo
from ..tools.registry import ToolRegistry
from .render import StepCompiler, plan_to_json, render_phase2


def _discover(registry: ToolRegistry, compiler_out: dict) -> tuple[dict, str]:
    """Resolve a step's concrete arguments without mutating anything."""
    op = compiler_out["op"]
    args = dict(compiler_out.get("args") or {})
    browser = registry.get("browser")

    if compiler_out["tool"] == "browser" and op == "navigate_path" and browser is not None:
        keyword = args.pop("keyword", "") or args.pop("path", "")
        res = browser.run("discover", keyword=keyword)
        if res.ok:
            data = res.data or {}
            args["start_url"] = args.get("start_url", "/dashboard")
            args["click_path"] = data.get("click_path", [])
            args["resolved_url"] = data.get("url", "")
            return {"tool": "browser", "op": "navigate_path", "args": args}, \
                   f"dry run: '{keyword}' resolves to {data.get('url')} via click path " \
                   f"{' > '.join(args['click_path'])}"
        return {"tool": "browser", "op": "navigate_path", "args": args}, \
               f"dry run: could NOT resolve navigation for '{keyword}' ({res.message}) — " \
               f"step kept as plan defect for Phase 3 healing"

    if compiler_out["tool"] == "browser" and op == "verify_text" and browser is not None:
        res = browser.run("discover", keyword=args.get("text", ""))
        if res.ok:
            data = res.data or {}
            args.setdefault("start_url", "/dashboard")
            args["click_path"] = data.get("click_path", [])
            args["resolved_url"] = data.get("url", "")
            return {"tool": "browser", "op": "verify_text", "args": args}, \
                   f"dry run: assertion '{args.get('text')}' found on {data.get('url')}"

    if compiler_out["tool"] == "cli" and op == "show":
        cli = registry.get("cli")
        cmd = args.get("command", "")
        if cli is not None and cmd:
            res = cli.run("exec", command=cmd)
            if res.ok:
                return {"tool": "cli", "op": "show", "args": args}, \
                       f"dry run: `{cmd}` accepted, {len(res.text.splitlines())} lines returned"
            return {"tool": "cli", "op": "show", "args": args}, \
                   f"dry run: `{cmd}` rejected ({res.message}) — will be healed in Phase 3"

    return {"tool": compiler_out["tool"], "op": op, "args": args}, "dry run: no discovery needed"


def execute_phase2(
    *,
    repo: Repo,
    registry: ToolRegistry,
    device: dict,
    clause: dict,
    scenario: dict,
    steps: list[dict],
    out_dir: Path,
) -> dict:
    compiler = StepCompiler()
    job_id = repo.create_job(
        device_id=device["id"], clause_id=clause["id"], scenario_id=scenario["id"],
        kind="dry_run", meta={"phase": 2, "scenario_no": scenario["scenario_no"]},
    )
    repo.set_job_state(job_id, "probing")

    pass_criteria = json.loads(scenario.get("pass_criteria_json") or "[]")
    fail_criteria = json.loads(scenario.get("fail_criteria_json") or "[]")
    mutation_scope = json.loads(scenario.get("mutation_scope_json") or "[]")
    scenario_view = {**scenario, "mutation_scope": mutation_scope}

    # Dry run is strictly read-only at the adapter boundary.
    registry.set_read_only(True)
    try:
        plan_steps: list[dict] = []
        notes: list[str] = []
        for st in steps:
            compiled = compiler.compile(st, scenario)
            resolved, note = _discover(registry, compiled)
            notes.append(f"Step {st['step_no']}: {note}")
            repo.log_action(job_id, kind="tool_call", tool=resolved["tool"], op=resolved["op"],
                            target=f"dryrun:step{st['step_no']}", request=resolved["args"],
                            outcome="ok", detail={"compiled_from": compiled.get("source", "")})

            plan_steps.append({
                "step_no": st["step_no"],
                "step_id": st["id"],
                "intent": st["action"],
                "expected": st.get("expected", ""),
                "tool": resolved["tool"],
                "op": resolved["op"],
                "args": resolved["args"],
                "lock": _infer_lock(st, resolved),
                "evidence": bool(st.get("expected")),
                "mutating": resolved["op"] in ("config", "click", "set_field", "action",
                                               "navigate_path"),
            })
    finally:
        registry.set_read_only(False)

    md = render_phase2(device, clause, scenario_view, plan_steps, pass_criteria, fail_criteria,
                       notes, job_id)
    path = Path(out_dir) / f"phase2_{device['model']}_{scenario['scenario_no']}.md".replace(" ", "")
    path.write_text(md, encoding="utf-8")

    plan_json = plan_to_json(device, clause, scenario_view, plan_steps, pass_criteria, fail_criteria)
    plan_json["dry_run_job"] = job_id
    plan_json["dry_run_notes"] = notes

    plan_id = repo.save_plan(
        device_id=device["id"], clause_id=clause["id"], scenario_id=scenario["id"],
        phase=2, content_md=md, content_json=plan_json, path_md=str(path), status="approved",
    )
    unresolved = [n for n in notes if "could NOT resolve" in n]
    repo.set_job_state(job_id, "passed",
                       summary=f"{len(plan_steps)} steps mapped, {len(unresolved)} unresolved")
    return {"job_id": job_id, "plan_id": plan_id, "path": str(path),
            "plan": plan_json, "notes": notes}


def _infer_lock(step: dict, resolved: dict) -> dict:
    """Derive the deterministic success proof for a step."""
    expected = (step.get("expected") or "").strip()
    if resolved["tool"] == "cli":
        cmd = resolved["args"].get("command", "")
        key = cmd.split()[-1] if cmd else "hostname"
        return {"kind": "terminal_regex", "value": rf"(?i){key}"}
    if resolved["op"] == "verify_text":
        return {"kind": "text_assert", "value": resolved["args"].get("text", "")}
    if expected:
        first = expected.split(".")[0].strip()
        if len(first) > 70:
            first = first[:70]
        return {"kind": "text_assert", "value": first}
    return {"kind": "text_assert", "value": resolved["args"].get("text", "")}
