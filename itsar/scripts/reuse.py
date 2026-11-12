"""Phase 4: script reuse across a device family, with v2 versioning.

Rules implemented here:
  * history belongs to the device it was produced on -- an old device's plans
    and scripts are read, never written
  * a rebind is a PARAMS substitution only; if the run needs more than that, the
    plan is healed and the result is stored as **v2** for the *new* device
"""
from __future__ import annotations

import json
from pathlib import Path

from ..repo import Repo, utcnow
from . import gen
from .runtime import ScriptResult, run_script

REBIND_KEYS = ("host", "model", "firmware", "credential_ref", "mgmt_protocol", "dut_profile")


def rebind(params: dict, new_device: dict) -> dict:
    out = dict(params)
    for k in REBIND_KEYS:
        if k == "dut_profile":
            out[k] = new_device.get("_profile_name", out.get(k, "old"))
        elif new_device.get(k):
            out[k] = new_device[k]
    out["rebound_at"] = utcnow()
    return out


def param_diff(old: dict, new: dict) -> dict:
    return {k: {"from": old.get(k), "to": new.get(k)}
            for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}


def run_rebound_script(
    *,
    repo: Repo,
    device: dict,
    scenario_row: dict,
    source_script: dict,
) -> dict:
    """Load a sibling device's script, rebind it, run it, log the run."""
    params = json.loads(source_script["params_json"])
    rebound = rebind(params, device)
    content = source_script["content"]
    ns = _load_script(content)

    job_id = repo.create_job(
        device_id=device["id"],
        clause_id=scenario_row["clause_id"],
        scenario_id=scenario_row["id"],
        script_id=source_script["id"],
        kind="reuse",
        meta={"source_script_id": source_script["id"], "source_device_id": source_script["device_id"],
              "param_diff": param_diff(params, rebound)},
    )
    repo.set_job_state(job_id, "running")
    repo.log_action(job_id, kind="state_change", op="reuse.start",
                    target=f"scenario {scenario_row['scenario_no']}", outcome="ok",
                    detail={"rebound_from_script": source_script["id"],
                            "param_diff": param_diff(params, rebound)})

    result: ScriptResult = _run(ns, rebound)
    repo.log_action(job_id, kind="tool_call", tool="script_runtime", op="run",
                    target=f"script:{source_script['id']}", outcome="ok" if result.ok else "error",
                    detail=result.to_dict())
    return {"job_id": job_id, "result": result, "params": rebound,
            "source_script": source_script, "content": content, "ns": ns}


def _run(ns: dict, params: dict) -> ScriptResult:
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_script("rebound", params, ns["STEPS"], ns.get("PASS_CRITERIA", []), ["--json"])
    try:
        data = json.loads(buf.getvalue() or "{}")
    except json.JSONDecodeError:
        return ScriptResult(False, None, "SCRIPT_ERROR", buf.getvalue())
    return ScriptResult(
        ok=bool(data.get("ok")), failed_step=data.get("failed_step"),
        code=data.get("code", ""), message=data.get("message", ""),
        observed=data.get("observed", ""), texts=[], criteria=data.get("criteria", []),
    )


def _load_script(content: str) -> dict:
    ns: dict = {}
    exec(compile(content, "<itsar-script>", "exec"), ns)  # noqa: S102 - trusted, generated code
    return ns


def save_v1_for_new_device(
    *, repo: Repo, device: dict, scenario_row: dict, source_script: dict,
    rebound_params: dict, job_id: str, out_dir: Path,
) -> int:
    """The v1 script ran clean on the new DUT: register it as that DUT's v1."""
    path = Path(out_dir) / f"scenario_{scenario_row['scenario_no']}_v1_{device['model']}.py".replace(" ", "")
    path.write_text(source_script["content"], encoding="utf-8")
    script_id = repo.add_script(
        device_id=device["id"], scenario_id=scenario_row["id"],
        content=source_script["content"], params=rebound_params, file_path=str(path),
        parent_script_id=source_script["id"], source_job_id=job_id,
        source_trace={"reused_from_device": source_script["device_id"],
                      "param_diff": param_diff(json.loads(source_script["params_json"]), rebound_params)},
        version=1,
    )
    repo.add_script_run(script_id=script_id, device_id=device["id"], job_id=job_id,
                        outcome="pass", heal_count=0,
                        diff={"mode": "rebind_only",
                              "param_diff": param_diff(json.loads(source_script["params_json"]),
                                                       rebound_params)})
    return script_id


def save_v2_for_new_device(
    *, repo: Repo, device: dict, clause: dict, scenario_row: dict, source_script: dict,
    exec_result: dict, out_dir: Path,
) -> int:
    """A rebind was not enough: store the healed path as v2, bound to the new DUT."""
    content, params, _meta = gen.generate(
        device=device, clause=clause, scenario=scenario_row,
        trace=exec_result["trace"],
        pass_criteria=exec_result["plan"].get("pass_criteria", []),
        job_id=exec_result["job_id"], version=2, generated=utcnow(),
        parent_note=(f"v2: healed from {source_script['device_id']}'s v{source_script['version']} "
                     f"after {len(exec_result['heals'])} heal event(s) on firmware "
                     f"{device.get('firmware')}"),
    )
    path = Path(out_dir) / f"scenario_{scenario_row['scenario_no']}_v2_{device['model']}.py".replace(" ", "")
    path.write_text(content, encoding="utf-8")
    script_id = repo.add_script(
        device_id=device["id"], scenario_id=scenario_row["id"], content=content, params=params,
        file_path=str(path), parent_script_id=source_script["id"],
        source_job_id=exec_result["job_id"],
        source_trace={"heals": exec_result["heals"], "trace": exec_result["trace"]},
        version=2,
    )
    repo.add_script_run(script_id=script_id, device_id=device["id"],
                        job_id=exec_result["job_id"], outcome="healed_pass",
                        heal_count=len(exec_result["heals"]),
                        diff={"mode": "healed", "heals": exec_result["heals"]})
    return script_id
