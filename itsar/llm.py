"""Optional LLM diagnosis for the self-healing loop.

The healer works without this module (deterministic strategies cover the common
CLI/UI drift cases). When an API key is configured, the LLM is asked to propose
a patch for cases the rules cannot express -- e.g. a menu that was restructured,
or a vendor that renamed a command into a sub-mode.

The contract is strict: the model must return JSON matching HealProposal, and
anything it proposes is still subject to the healer's safety checks
(mutation-scope, attempt budget, approval gate). The LLM never gets to bypass
those.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DIAGNOSE_PROMPT = """You are the self-healing component of an ITSAR compliance test agent.

A plan step failed during execution against a live network device. Diagnose the
cause and propose ONE minimal patch that lets the step complete its original
intent. Do not change the intent, do not skip the step, and do not propose
anything outside the scenario's declared mutation scope.

DEVICE
  model={model} firmware={firmware} host={host}

CLAUSE {clause_no} / SCENARIO {scenario_no} — {scenario_title}
  allowed mutations: {mutation_scope}

FAILED STEP {step_no}
  intent: {intent}
  expected: {expected}
  tool/op: {tool}/{op}
  args: {args}

FAILURE
  code={code}
  message={message}
  observed text (truncated): {text}

CONTEXT
  attempt {attempt} of {max_attempts}
  candidates/available elements reported by the tool: {meta}

Respond with ONLY this JSON object:
{{"diagnosis": "<one sentence root cause>",
  "strategy": "<SELECTOR_RENAME|MENU_RESTRUCTURE|COMMAND_VARIANT|TIMEOUT|FEATURE_ABSENT>",
  "patch": {{"kind": "patch_step|replace_step|mark_not_supported",
             "changes": {{"args": {{...}}, "op": "...", "tool": "..."}},
             "approval_required": false}},
  "confidence": 0.0}}
"""


class LlmDiagnosis:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def diagnose(self, context: dict) -> dict | None:
        if not self.enabled:
            return None
        prompt = DIAGNOSE_PROMPT.format(**context)
        body = {
            "model": self.model,
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

        text = _extract_text(payload)
        return _parse_json_object(text)


def _extract_text(payload: dict[str, Any]) -> str:
    if "content" in payload and isinstance(payload["content"], list):
        return "".join(b.get("text", "") for b in payload["content"] if isinstance(b, dict))
    if "choices" in payload:
        return payload["choices"][0]["message"]["content"]
    return ""


def _parse_json_object(text: str) -> dict | None:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
