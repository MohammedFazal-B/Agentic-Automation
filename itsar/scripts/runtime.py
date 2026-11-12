"""Runtime used by generated v1/v2 scenario scripts.

Runs the script's STEPS against whatever PARAMS points at:
  * mode="live" -> real SSH CLI + Playwright/Chrome browser
  * mode="mock" -> deterministic simulated DUT (used by the demo and by CI)

The runner is deliberately *strict*: it reports the first failing step with the
tool's error code and the observed screen, because that failure is precisely
the input the Phase-4 healing path needs to produce a v2.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScriptResult:
    ok: bool
    failed_step: int | None = None
    code: str = ""
    message: str = ""
    observed: str = ""
    texts: list[str] = field(default_factory=list)
    criteria: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "failed_step": self.failed_step, "code": self.code,
            "message": self.message, "observed": self.observed,
            "criteria": self.criteria,
        }


def build_registry(params: dict, credentials: dict | None = None):
    """Construct adapters for the script's target DUT."""
    from ..tools.registry import ToolRegistry

    reg = ToolRegistry()
    if params.get("mode") == "live":
        from ..tools.browser import PlaywrightMcpBrowserAdapter
        from ..tools.cli import SshCliAdapter

        reg.register(SshCliAdapter(params["host"], params["credential_ref"],
                                   credentials or {}))
        reg.register(PlaywrightMcpBrowserAdapter())
    else:
        from ..tools.mock import (MockBrowserAdapter, MockCliAdapter, MockDut,
                                  NEW_FIRMWARE, OLD_FIRMWARE)

        profile = dict(NEW_FIRMWARE if params.get("dut_profile") == "new" else OLD_FIRMWARE)
        if params.get("firmware"):
            profile["firmware"] = params["firmware"]
        if params.get("model"):
            profile["hostname"] = params["model"]
        dut = MockDut(profile=profile)
        reg.register(MockCliAdapter(dut))
        reg.register(MockBrowserAdapter(dut))
    return reg


def _expand(step: dict) -> list[tuple[str, str, dict]]:
    tool, op = step["tool"], step["op"]
    args = dict(step.get("args") or {})
    if tool == "browser":
        if op == "navigate_path":
            out = [("browser", "goto", {"url": args.get("start_url", "/dashboard")})]
            out += [("browser", "click", {"label": lb}) for lb in args.get("click_path", [])]
            return out
        if op == "verify_text":
            out = []
            if args.get("click_path"):
                out.append(("browser", "goto", {"url": args.get("start_url", "/dashboard")}))
                out += [("browser", "click", {"label": lb}) for lb in args["click_path"]]
            out.append(("browser", "read_text", {}))
            return out
        return [("browser", op, args)]
    if op == "show":
        return [(tool, "exec", {"command": args.get("command", "")})]
    if op == "config":
        return [(tool, "config", {"command": args.get("command", "")})]
    return [(tool, op, args)]


def _verify_lock(lock: dict, text: str) -> tuple[bool, str]:
    from ..evidence import StateLock

    return StateLock.from_dict(lock).verify(text)


def run_script(script_ref, params: dict, steps: list[dict], pass_criteria: list[str],
               argv: list[str] | None = None, credentials: dict | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--host", default=None)
    parser.add_argument("--json", action="store_true")
    args, _unknown = parser.parse_known_args(argv)

    params = dict(params)
    if args.host:
        params["host"] = args.host

    registry = build_registry(params, credentials)
    texts: list[str] = []

    for step in steps:
        combined = ""
        failed = None
        for tool, op, sargs in _expand(step):
            adapter = registry.get(tool)
            if adapter is None:
                failed = (step["step_no"], "UNKNOWN_TOOL", f"tool {tool} not available", combined)
                break
            res = adapter.run(op, **sargs)
            combined += ("\n" + res.text) if res.text else ""
            if not res.ok:
                failed = (step["step_no"], res.code, res.message, combined)
                break
        if failed:
            result = ScriptResult(False, failed[0], failed[1], failed[2], failed[3], texts)
            return _emit(result, params, args.json)

        ok, detail = _verify_lock(step.get("lock") or {}, combined)
        texts.append(combined)
        if not ok:
            result = ScriptResult(False, step["step_no"], "LOCK_NOT_VERIFIED", detail, combined, texts)
            return _emit(result, params, args.json)

    criteria = evaluate(pass_criteria, texts)
    result = ScriptResult(all(c["met"] for c in criteria), None, "", "", "", texts, criteria)
    return _emit(result, params, args.json)


def evaluate(criteria: list[str], texts: list[str]) -> list[dict]:
    import re

    blob = "\n".join(texts).lower()
    out: list[dict] = []
    for c in criteria:
        numbers = re.findall(r"\b\d{1,3}\b", str(c))
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", str(c))
                 if w.lower() not in {"the", "and", "that", "with", "must", "should", "shall",
                                      "have", "been", "this", "from", "into", "when", "then",
                                      "than", "only", "also", "upon", "does", "will", "least",
                                      "value", "config", "configured", "setting", "settings",
                                      "enabled"}]
        hits = [w for w in words if w in blob]
        nums_ok = all(n in blob for n in numbers) if numbers else True
        met = nums_ok and (len(hits) >= max(2, int(len(set(words)) * 0.4)))
        out.append({"criterion": c, "met": bool(met), "matched_terms": sorted(set(hits))[:8]})
    return out


def _emit(result: ScriptResult, params: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] scenario {params.get('clause_no')}/{params.get('scenario_no')} "
              f"on {params.get('host')}")
        if not result.ok:
            print(f"  failed step {result.failed_step}: {result.code} {result.message}")
            print("  observed:\n" + "\n".join("    " + ln for ln in (result.observed or "").splitlines()[-12:]))
        for c in result.criteria:
            print(f"  [{'x' if c['met'] else ' '}] {c['criterion']}")
    return 0 if result.ok else 2
