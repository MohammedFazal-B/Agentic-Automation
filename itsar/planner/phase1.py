"""Phase 1: active precondition verification, remediation, and plan generation."""
from __future__ import annotations

from pathlib import Path

from ..evidence import EvidenceGuard, StateLock
from ..repo import Repo, utcnow
from ..tools.base import ToolAdapter, ToolResult
from ..tools.registry import ToolRegistry
from .render import _tbl, render_phase1

# Requirement -> the exact probe method, and the exact remediation method.
# Kept as data so a new device family extends it without touching logic.
REMEDIATION_CATALOG: dict[str, dict] = {
    "ssh": {
        "probe": {"tool": "cli", "op": "probe_port", "args": {"port": 22}},
        "verify": {"tool": "cli", "op": "exec", "args": {"command": "show ssh"}},
        "enable": {"tool": "cli", "op": "enable_ssh", "args": {}},
        "enable_note": "console/out-of-band: `ssh server enable` then `write memory`",
        "post_check": {"tool": "cli", "op": "probe_port", "args": {"port": 22}},
    },
    "https": {
        "probe": {"tool": "cli", "op": "probe_port", "args": {"port": 443}},
        "verify": {"tool": "browser", "op": "goto", "args": {"url": "/dashboard"}},
        "enable": {"tool": "browser", "op": "goto", "args": {"url": "/dashboard"}},
        "enable_note": "GUI management plane; enable via `http server secure` if closed",
        "post_check": {"tool": "cli", "op": "probe_port", "args": {"port": 443}},
    },
    "telnet": {
        "probe": {"tool": "cli", "op": "probe_port", "args": {"port": 23}},
        "verify": {"tool": "cli", "op": "exec", "args": {"command": "show ip telnet"}},
        "enable": {"tool": "cli", "op": "config", "args": {"command": "telnet server disable"}},
        "enable_note": "telnet must be DISABLED; remediation disables it if found enabled",
        "post_check": {"tool": "cli", "op": "probe_port", "args": {"port": 23}},
    },
}


def _run(registry: ToolRegistry, tool: str, op: str, args: dict) -> ToolResult:
    ad: ToolAdapter | None = registry.get(tool)
    if ad is None:
        return ToolResult.failure("UNKNOWN", f"tool '{tool}' not registered")
    return ad.run(op, **args)


def execute_phase1(
    *,
    repo: Repo,
    registry: ToolRegistry,
    device: dict,
    preconditions: dict,
    objectives: list[str],
    guard: EvidenceGuard,
    out_dir: Path,
) -> dict:
    """Probe -> remediate -> re-probe -> write and store the Phase 1 plan."""
    job_id = repo.create_job(
        device_id=device["id"],
        clause_id=None,
        kind="precondition",
        meta={"phase": 1, "preconditions": preconditions},
    )
    repo.set_job_state(job_id, "probing")
    repo.log_action(job_id, kind="state_change", op="phase1.start", target=device["host"],
                    outcome="ok", detail={"preconditions": preconditions})

    probes: list[dict] = []
    required = list(preconditions.get("protocols", [])) + list(preconditions.get("interfaces", []))

    for req in required:
        name = (req.get("name") or "").lower()
        cat = REMEDIATION_CATALOG.get(name)
        record = {
            "requirement": req.get("name", name),
            "target": f"tcp/{req.get('port')}" if req.get("port") else req.get("type", "interface"),
            "method": "", "observed": "", "status": "unknown",
            "remediation": "", "post_state": "", "evidence_path": "",
        }

        if cat is None:
            record.update(status="not_supported",
                          detail=f"no probe/remediation catalogue entry for '{name}' on this platform",
                          method="n/a")
            probes.append(record)
            continue

        probe = _run(registry, cat["probe"]["tool"], cat["probe"]["op"], cat["probe"]["args"])
        record["method"] = f"{cat['probe']['tool']}:{cat['probe']['op']}({cat['probe']['args']})"
        repo.log_action(job_id, kind="tool_call", tool=cat["probe"]["tool"], op=cat["probe"]["op"],
                        target=record["target"], request=cat["probe"]["args"],
                        response=probe.text, outcome="ok" if probe.ok else "error",
                        latency_ms=probe.latency_ms, detail={"code": probe.code})

        # telnet is the inverted case: open is BAD.
        inverted = name == "telnet"
        healthy = (not probe.ok) if inverted else probe.ok
        record["observed"] = "open" if probe.ok else "closed"

        if healthy:
            record["status"] = "reachable" if not inverted else "compliant_disabled"
            record["post_state"] = record["observed"]
        elif not req.get("required", True) and not inverted:
            record["status"] = "not_required"
        else:
            # ---- remediation
            rem = _run(registry, cat["enable"]["tool"], cat["enable"]["op"], cat["enable"]["args"])
            repo.log_action(job_id, kind="tool_call", tool=cat["enable"]["tool"],
                            op=cat["enable"]["op"], target=record["target"],
                            request=cat["enable"]["args"],
                            outcome="ok" if rem.ok else "error",
                            latency_ms=rem.latency_ms,
                            detail={"code": rem.code, "remediation": True})
            record["remediation"] = f"`{cat['enable_note']}`" if rem.ok else ""
            record["remediated"] = rem.ok
            if rem.ok:
                post = cat["post_check"]
                after = _run(registry, post["tool"], post["op"], post["args"])
                repo.log_action(job_id, kind="tool_call", tool=post["tool"], op=post["op"],
                                target=record["target"], request=post["args"],
                                response=after.text, outcome="ok" if after.ok else "error",
                                latency_ms=after.latency_ms)
                record["post_state"] = "open" if after.ok else "closed"
                record["status"] = "remediated" if (after.ok != inverted) else "blocked"
            else:
                record["status"] = "blocked"
                record["detail"] = rem.message

        probes.append(record)

    # Precondition evidence: capture the post-remediation CLI state (and only that).
    cli = registry.get("cli")
    if cli is not None:
        cfg = cli.run("exec", command="show running-config")
        lock = StateLock("terminal_regex", r"hostname\s+\S+")
        ok, detail = lock.verify(cfg.text, cfg)
        try:
            ev = guard.capture(job_id=job_id, step_id=None, step_no=900,
                               adapter=cli, lock=lock, lock_ok=ok, lock_detail=detail,
                               screen_text=cfg.text,
                               title=f"Phase 1 — running config after remediation ({device['host']})",
                               kind="terminal")
            for p in probes:
                if p.get("remediated"):
                    p["evidence_path"] = ev["path"]
        except Exception as exc:  # EvidenceRejected
            repo.log_action(job_id, kind="lock_fail", op="phase1.evidence",
                            outcome="refused", detail={"error": str(exc)})

    md = render_phase1({**device, "_now": utcnow()}, preconditions, probes, objectives, job_id)
    path = Path(out_dir) / f"phase1_{device['model']}_{device['firmware']}.md".replace(" ", "")
    path.write_text(md, encoding="utf-8")

    plan_json = {
        "schema": "itsar.phase1.v1",
        "device": {k: device.get(k) for k in ("model", "firmware", "host", "credential_ref")},
        "preconditions": preconditions,
        "objectives": objectives,
        "probes": probes,
        "job_id": job_id,
    }
    clause_id = device["_clause_id"]
    plan_id = repo.save_plan(
        device_id=device["id"], clause_id=clause_id, phase=1,
        content_md=md, content_json=plan_json, path_md=str(path), status="approved",
    )

    blocked = [p for p in probes if p["status"] == "blocked"]
    state = "blocked" if blocked else "passed"
    repo.set_job_state(job_id, state, summary=f"{len(probes)} preconditions probed, "
                                             f"{len(blocked)} blocked")
    return {"job_id": job_id, "plan_id": plan_id, "path": str(path),
            "probes": probes, "state": state}
