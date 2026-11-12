"""Phase 3 self-healing: diagnose a failed step, produce a plan patch.

Two rules keep this safe enough to run against a production device:

  1. **Bounded.** At most ``max_heal_attempts`` patches per step, and each
     patch must differ from every previous patch for that step (no loops).

  2. **Scoped.** A patch may change navigation, selectors, timeouts, or command
     syntax. It may NOT introduce a mutating operation that the scenario did not
     declare in its mutation scope. Those are escalated as ``approval_required``
     and the run stops at ``blocked`` instead of quietly reconfiguring a device.

Every heal is written to ``heal_event`` *and* materialises a new immutable plan
version, so "what the agent changed and why" is answerable after the fact.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any

from .llm import LlmDiagnosis
from .repo import Repo
from .tools.base import ToolErrorCode, ToolResult
from .tools.registry import ToolRegistry

MUTATING_OPS = {"config", "click", "set_field", "action", "navigate_path"}


@dataclass
class HealProposal:
    diagnosis: str
    strategy: str
    patch: dict = field(default_factory=dict)
    approval_required: bool = False
    confidence: float = 0.0
    origin: str = "rule"          # rule | llm

    def to_dict(self) -> dict:
        return asdict(self)


class Healer:
    def __init__(self, repo: Repo, registry: ToolRegistry, llm: LlmDiagnosis | None = None,
                 max_attempts: int = 3):
        self.repo = repo
        self.registry = registry
        self.llm = llm
        self.max_attempts = max_attempts
        self._history: dict[tuple[str, int], list[str]] = {}

    # ------------------------------------------------------------- diagnose
    def diagnose(
        self,
        *,
        job_id: str,
        device: dict,
        clause: dict,
        scenario: dict,
        step: dict,
        result: ToolResult,
        attempt: int,
    ) -> HealProposal:
        proposal = self._rule_based(step, result)

        if proposal is None and self.llm and self.llm.enabled:
            proposal = self._llm_based(device, clause, scenario, step, result, attempt)
            if proposal:
                proposal.origin = "llm"

        if proposal is None:
            proposal = HealProposal(
                diagnosis=f"unrecognised failure ({result.code or 'unknown'}) after "
                          f"{attempt} attempt(s): {result.message}",
                strategy="UNCLASSIFIED",
                patch={"kind": "abort", "changes": {}},
                confidence=0.0,
            )

        # --- safety gates -------------------------------------------------
        proposal.patch = _ensure_patch_shape(proposal.patch)
        if self._expands_mutation_scope(step, proposal.patch, scenario):
            proposal.approval_required = True
            proposal.diagnosis += " | patch needs a mutation outside the declared scope"
        if self._is_repeat(job_id, step["step_no"], proposal.patch):
            proposal.diagnosis += " | duplicate of an earlier patch"
            proposal.patch = {"kind": "abort", "changes": {}}
        return proposal

    # ---------------------------------------------------------- rule engine
    def _rule_based(self, step: dict, result: ToolResult) -> HealProposal | None:
        code = result.code
        args = dict(step.get("args") or {})
        meta = result.meta or {}

        # 1. A UI label moved / was renamed (firmware drift).
        if code == ToolErrorCode.SELECTOR_NOT_FOUND:
            candidates = meta.get("candidates") or []
            click_path = list(args.get("click_path") or [])
            label = meta.get("label") or (click_path[-1] if click_path else args.get("label", ""))
            idx = meta.get("failed_click_index")
            if candidates:
                if isinstance(idx, int) and 0 <= idx < len(click_path):
                    click_path[idx] = candidates[0]
                elif click_path:
                    click_path[-1] = candidates[0]
                return HealProposal(
                    diagnosis=f"label {label!r} no longer exists on this firmware; "
                              f"nearest match is {candidates[0]!r}",
                    strategy="SELECTOR_RENAME",
                    patch={"kind": "patch_step",
                           "changes": {"args": {"click_path": click_path,
                                                "label": candidates[0]}}},
                    confidence=0.9,
                )
            # 2. Menu restructured: re-discover the path from the step intent.
            return HealProposal(
                diagnosis=f"no element matched {label!r}; the menu was likely restructured",
                strategy="MENU_RESTRUCTURE",
                patch={"kind": "rediscover", "changes": {"keyword": _keyword(step)}},
                confidence=0.6,
            )

        # 3. Command syntax differs on this platform.
        if code == ToolErrorCode.UNKNOWN_COMMAND:
            cmd = args.get("command", "")
            alt = _command_variants(cmd)
            if alt:
                return HealProposal(
                    diagnosis=f"`{cmd}` is not accepted by this firmware; trying platform variant",
                    strategy="COMMAND_VARIANT",
                    patch={"kind": "patch_step", "changes": {"args": {"command": alt}}},
                    confidence=0.7,
                )

        # 4. Service was found disabled at execution time.
        if code in (ToolErrorCode.SERVICE_DISABLED, ToolErrorCode.NETWORK_UNREACHABLE):
            return HealProposal(
                diagnosis="required management service is disabled; enable out-of-band "
                          "then re-probe",
                strategy="SERVICE_ENABLE",
                patch={"kind": "remediate_service", "changes": {"op": "enable_ssh"}},
                confidence=0.85,
            )

        # 5. Timeout: retry with a longer budget (op is unchanged, so no scope risk).
        if code == ToolErrorCode.TIMEOUT:
            return HealProposal(
                diagnosis="operation exceeded the settle window; retrying with a longer timeout",
                strategy="TIMEOUT",
                patch={"kind": "patch_step",
                       "changes": {"args": {"timeout_multiplier": 2}}},
                confidence=0.5,
            )

        # 6. Genuinely absent capability: NOT a failure.
        if code == ToolErrorCode.UNSUPPORTED_FEATURE:
            return HealProposal(
                diagnosis=f"capability is absent from firmware {result.meta.get('capability', '')}",
                strategy="FEATURE_ABSENT",
                patch={"kind": "mark_not_supported",
                       "changes": {"detail": result.message}},
                confidence=0.95,
            )

        # 7. State lock failed even though the action "succeeded".
        if code == "LOCK_NOT_VERIFIED":
            return HealProposal(
                diagnosis="action returned success but the target state was not proven; "
                          "re-resolving the assertion from the live screen",
                strategy="LOCK_REWIDEN",
                patch={"kind": "rewiden_lock", "changes": {"keyword": _keyword(step)}},
                confidence=0.55,
            )
        return None

    # ------------------------------------------------------------ llm path
    def _llm_based(self, device, clause, scenario, step, result, attempt) -> HealProposal | None:
        ctx = {
            "model": device.get("model"), "firmware": device.get("firmware"),
            "host": device.get("host"), "clause_no": clause.get("clause_no"),
            "scenario_no": scenario.get("scenario_no"),
            "scenario_title": scenario.get("title"),
            "mutation_scope": scenario.get("mutation_scope_json", "[]"),
            "step_no": step.get("step_no"), "intent": step.get("intent"),
            "expected": step.get("expected"), "tool": step.get("tool"), "op": step.get("op"),
            "args": step.get("args"), "code": result.code, "message": result.message,
            "text": (result.text or "")[:1500], "attempt": attempt,
            "max_attempts": self.max_attempts, "meta": result.meta,
        }
        out = self.llm.diagnose(ctx)
        if not out:
            return None
        patch = out.get("patch") or {}
        return HealProposal(
            diagnosis=out.get("diagnosis", "llm diagnosis"),
            strategy=out.get("strategy", "LLM"),
            patch=patch,
            approval_required=bool(patch.get("approval_required")),
            confidence=float(out.get("confidence") or 0.5),
        )

    # ------------------------------------------------------------- helpers
    def _expands_mutation_scope(self, step: dict, patch: dict, scenario: dict) -> bool:
        if not step.get("mutating"):
            return False
        new_op = (patch.get("changes") or {}).get("op")
        if new_op and new_op in MUTATING_OPS and new_op != step.get("op"):
            return True
        return False

    def _is_repeat(self, job_id: str, step_no: int, patch: dict) -> bool:
        import json
        key = (job_id, step_no)
        sig = json.dumps(patch, sort_keys=True)
        seen = self._history.setdefault(key, [])
        if sig in seen:
            return True
        seen.append(sig)
        return False


# ---------------------------------------------------------------------------
# patch application
# ---------------------------------------------------------------------------

def apply_patch(plan: dict, step_no: int, patch: dict, registry: ToolRegistry) -> dict:
    """Return a NEW plan dict with the patch applied. Never mutates the input."""
    new_plan = copy.deepcopy(plan)
    target = next((s for s in new_plan["steps"] if s["step_no"] == step_no), None)
    if target is None:
        return new_plan

    kind = patch.get("kind")
    changes = patch.get("changes") or {}

    if kind in ("patch_step", "replace_step"):
        for k, v in (changes.get("args") or {}).items():
            target.setdefault("args", {})[k] = v
        if changes.get("op"):
            target["op"] = changes["op"]
        if changes.get("tool"):
            target["tool"] = changes["tool"]
        if changes.get("lock"):
            target["lock"] = changes["lock"]

    elif kind == "rediscover":
        browser = registry.get("browser")
        if browser is not None:
            res = browser.run("discover", keyword=changes.get("keyword", ""))
            if res.ok:
                target["args"]["click_path"] = (res.data or {}).get("click_path", [])
                target["args"]["resolved_url"] = (res.data or {}).get("url", "")

    elif kind == "rewiden_lock":
        browser = registry.get("browser")
        if browser is not None:
            res = browser.run("discover", keyword=changes.get("keyword", ""))
            if res.ok:
                title = (res.data or {}).get("title", changes.get("keyword", ""))
                target["lock"] = {"kind": "text_assert", "value": title}
                target["args"].setdefault("click_path", (res.data or {}).get("click_path", []))

    elif kind == "remediate_service":
        target["_remediate"] = changes.get("op", "enable_ssh")

    elif kind == "mark_not_supported":
        target["_not_supported"] = changes.get("detail", "feature absent")

    target.setdefault("_healed", []).append(patch)
    return new_plan


def _keyword(step: dict) -> str:
    for k in ("resolved_url", "text", "command"):
        v = (step.get("args") or {}).get(k)
        if v:
            return str(v).replace("/", " ").replace("-", " ").strip()
    return " ".join((step.get("intent") or "").split()[:4])


def _command_variants(cmd: str) -> str:
    c = " ".join(cmd.split())
    low = c.lower()
    variants = []
    if low.startswith("show "):
        variants = [c.replace("show ", "display ", 1), c + " config", c.replace("show ", "", 1)]
    elif low.startswith("display "):
        variants = [c.replace("display ", "show ", 1)]
    elif low.startswith("get "):
        variants = [c.replace("get ", "show ", 1)]
    return variants[0] if variants else ""


def _ensure_patch_shape(patch: dict) -> dict:
    """Reject malformed proposals rather than treating them as executable patches."""
    allowed = {'abort', 'patch_step', 'replace_step', 'rediscover', 'rewiden_lock',
               'remediate_service', 'mark_not_supported'}
    if not isinstance(patch, dict) or patch.get('kind') not in allowed or not isinstance(patch.get('changes', {}), dict):
        return {'kind': 'abort', 'changes': {}}
    return {'kind': patch['kind'], 'changes': patch.get('changes', {})}
