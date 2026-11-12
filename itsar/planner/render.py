"""Planner: renders plan objects to Markdown and turns approved-report steps
into concrete, tool-bound plan steps.

The Markdown is the human-reviewable artefact that goes into the DB; the JSON
sibling is what the executor loads. They are generated from one source so they
can never drift.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Step compilation: "intent" -> concrete tool op, with dry-run validation.
# ---------------------------------------------------------------------------

BROWSER_PATTERNS: list[tuple[str, dict]] = [
    (r"navigate to (?P<path>[^.;]+)", {"op": "navigate_path"}),
    (r"click (?P<label>[^.;]+)", {"op": "click"}),
    (r"set (?P<field>[^.;]+?) to (?P<value>[^.;]+)", {"op": "set_field"}),
    (r"verify (?P<text>[^.;]+)", {"op": "verify_text"}),
]

CLI_PATTERNS: list[tuple[str, dict]] = [
    (r"(?i)^(show|display|get) .+", {"op": "show"}),
    (r"(?i)^(set|configure|enable|disable) .+", {"op": "config"}),
]


class StepCompiler:
    """Maps a scenario step's prose to an initial op proposal.

    The proposal is *validated by the dry run* before it is allowed into an
    executable plan. If the approved report already carries structured hints
    (``tool``/``op``/``args``), those win -- they are the parsed ground truth.
    """

    def compile(self, step: dict, scenario: dict) -> dict:
        if step.get("tool") and step.get("op"):
            return {
                "tool": step["tool"],
                "op": step["op"],
                "args": dict(step.get("args") or {}),
                "source": "report_hint",
            }
        hint_tool = (step.get("tool_hint") or "").lower()
        action = step.get("action", "")
        locator = step.get("locator_hint") or ""

        if hint_tool.startswith("cli") or re.match(r"(?i)^(show|display|set|enable|disable|configure)\b", action):
            op = "config" if re.match(r"(?i)^(set|enable|disable|configure)\b", action) else "show"
            cmd = locator or action
            return {"tool": "cli", "op": op, "args": {"command": cmd.strip()}, "source": "compiled"}

        if re.match(r"(?i)^(verify|confirm|check)\b", action):
            return {"tool": "browser", "op": "verify_text",
                    "args": {"text": locator or _subject(action)}, "source": "compiled"}

        m = re.search(r"(?i)navigate to (?P<path>.+?)(?: and |$)", action)
        if m:
            return {"tool": "browser", "op": "navigate_path",
                    "args": {"keyword": locator or m.group("path").strip(),
                             "start_url": "/dashboard"},
                    "source": "compiled"}

        return {"tool": "browser", "op": "verify_text",
                "args": {"text": locator or _subject(action)}, "source": "unresolved"}


def _subject(action: str) -> str:
    words = action.split()
    return " ".join(words[:4]) if words else action


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _tbl(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out)


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", "<br>")


def render_phase1(device: dict, preconditions: dict, probes: list[dict],
                  objectives: list[str], job_id: str) -> str:
    required = preconditions.get("protocols", []) + preconditions.get("interfaces", [])
    rem = [p for p in probes if p.get("remediated")]
    unsup = [p for p in probes if p.get("status") == "not_supported"]

    md = [f"# Phase 1 — Precondition Verification Plan",
          "",
          f"**Device:** {device.get('model')}  |  **Firmware:** {device.get('firmware')}  "
          f"|  **Host:** {device.get('host')}  ",
          f"**Device family:** {(device.get('family') or {}).get('name', 'n/a')}  "
          f"|  **Job ID:** `{job_id}`  ",
          f"**Generated:** {device.get('_now', '')}",
          "",
          "## 1. What the approved report required",
          ""]
    if not required:
        md.append("_No explicit protocol/interface preconditions were declared._")
    for p in preconditions.get("protocols", []):
        md.append(f"- **Protocol** `{p.get('name')}` on tcp/{p.get('port')} — "
                  f"required: {p.get('required', True)} — purpose: {p.get('purpose', '')}")
    for i in preconditions.get("interfaces", []):
        md.append(f"- **Interface** `{i.get('name')}` ({i.get('type', 'mgmt')}) — "
                  f"required: {i.get('required', True)}")
    for s in preconditions.get("services", []):
        md.append(f"- **Service/Browser** `{s.get('name')}` — required: {s.get('required', True)}")

    md += ["", "## 2. Objectives of this phase", ""]
    md += [f"{n}. {o}" for n, o in enumerate(objectives, 1)]

    md += ["", "## 3. Live probe results", "",
           _tbl(["Requirement", "Target", "Method / exact command",
                 "Observed", "Status", "Remediation applied"],
                [[p.get("requirement"), p.get("target"), f"`{p.get('method')}`",
                  p.get("observed", ""), p.get("status", ""),
                  p.get("remediation") or "—"] for p in probes])]

    if rem:
        md += ["", "## 4. Remediation actions performed", ""]
        for p in rem:
            md += [f"### {p.get('requirement')} — `{p.get('target')}`",
                   f"- **Detected state:** {p.get('observed')}",
                   f"- **Remediation method:** `{p.get('remediation')}`",
                   f"- **Post-remediation verification:** `{p.get('method')}` → {p.get('post_state')}",
                   f"- **Evidence:** `{p.get('evidence_path') or 'terminal transcript logged under job'}`",
                   ""]
    if unsup:
        md += ["", "## 5. Features marked **Not Supported** on this firmware", ""]
        for p in unsup:
            md += [f"- `{p.get('requirement')}` — {p.get('detail')}. "
                   f"Recorded as **Not Supported**, not as a failure "
                   f"(clause verdict will carry this status forward)."]

    md += ["", "## 6. Interfaces, ports and protocols actually used", "",
           _tbl(["Layer", "Identifier", "Port", "Credential reference", "Notes"],
                [["Management GUI", f"https://{device.get('host')}", 443,
                  device.get("credential_ref"), "Google Chrome via Playwright MCP"],
                 ["Management CLI", f"ssh {device.get('host')}", 22,
                  device.get("credential_ref"), "SSHv2 only; telnet prohibited by policy"],
                 ["Console", "serial / local", 0, "n/a",
                  "Used only for out-of-band remediation when SSH is disabled"]]),
           "", "## 7. Exit criteria for this phase", "",
           "- Every **required** precondition is `reachable` or `remediated`.",
           "- Any absent capability is explicitly marked `not_supported` with evidence.",
           "- No further precondition work is needed before Phase 2 begins.", ""]
    return "\n".join(md)


def render_phase2(device: dict, clause: dict, scenario: dict, steps: list[dict],
                  pass_criteria: list[str], fail_criteria: list[str],
                  dry_run_notes: list[str], job_id: str) -> str:
    md = [f"# Phase 2 — Scenario Execution Plan",
          "",
          f"**Device:** {device.get('model')} ({device.get('firmware')})  |  **Host:** {device.get('host')}  ",
          f"**Clause:** {clause.get('clause_no')} — {clause.get('title')}  "
          f"|  **Scenario:** {scenario.get('scenario_no')} — {scenario.get('title')}  ",
          f"**Dry-run job:** `{job_id}`",
          "",
          "## 1. Intent behind this scenario",
          "",
          scenario.get("intent") or scenario.get("title", ""),
          "",
          "## 2. Pass criteria (from approved report)", ""]
    md += [f"- [ ] {c}" for c in pass_criteria] or ["- _none declared_"]
    md += ["", "## 3. Fail criteria", ""]
    md += [f"- {c}" for c in fail_criteria] or ["- _none declared_"]

    md += ["", "## 4. Tool binding and navigation map", "",
           _tbl(["#", "Step intent", "Tool / MCP", "Operation", "Arguments",
                 "State lock (success proof)", "Evidence"],
                [[s["step_no"], s.get("intent", ""), f"`{s.get('tool')}`", f"`{s.get('op')}`",
                  f"`{json.dumps(s.get('args', {}))[:110]}`",
                  f"{s.get('lock', {}).get('kind')}: `{s.get('lock', {}).get('value')}`",
                  "yes" if s.get("evidence") else "—"] for s in steps])]

    md += ["", "## 5. Dry-run findings (non-destructive)", ""]
    md += [f"- {n}" for n in dry_run_notes] or ["- No deviations discovered."]

    md += ["", "## 6. Mutation scope", "",
           "This plan may only change: " +
           (", ".join(f"`{m}`" for m in scenario.get("mutation_scope", [])) or "_nothing (read-only scenario)_") +
           ".", "", "## 7. Evidence policy for this scenario", "",
           "A screenshot is captured only after the **State lock** column proves success. "
           "Error pages, missing-dependency warnings, auth walls and loading frames are "
           "refused by the evidence guard and logged as rejections — never attached to the report.",
           "", "## 8. Post-execution", "",
           "- Evaluate pass/fail criteria strictly from captured evidence.",
           "- On success: emit the scenario as a **v1** executable script bound to this DUT.",
           "- On a plan defect: heal, store the healed plan as the next version, resume.", ""]
    return "\n".join(md)


def plan_to_json(device: dict, clause: dict, scenario: dict, steps: list[dict],
                 pass_criteria: list[str], fail_criteria: list[str]) -> dict:
    return {
        "schema": "itsar.plan.v1",
        "device": {"model": device.get("model"), "firmware": device.get("firmware"),
                   "host": device.get("host"), "credential_ref": device.get("credential_ref")},
        "clause": {"clause_no": clause.get("clause_no"), "title": clause.get("title")},
        "scenario": {"scenario_no": scenario.get("scenario_no"), "title": scenario.get("title"),
                     "mutation_scope": scenario.get("mutation_scope", [])},
        "pass_criteria": pass_criteria,
        "fail_criteria": fail_criteria,
        "steps": steps,
    }
