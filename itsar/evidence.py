"""Evidence guard -- the enforcement point for directive 4.

Rule: a screenshot exists if and only if a deterministic state lock proved the
step's target state was reached. Everything else is logged as a rejection and
the run continues (a rejected capture is an execution problem, not an evidence
problem -- and it must never pollute the report).

The guard refuses:
  * captures where the state lock was not verified
  * captures whose visible text matches an error / dependency / loading signature
  * captures on an auth wall (session expired, login page)
  * duplicate frames (same sha256 already stored for the job)
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repo import Repo, utcnow
from .tools.base import ToolAdapter, ToolResult


class EvidenceRejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# Screens of these classes must NEVER become evidence.
FORBIDDEN_SCREEN_SIGNATURES: list[tuple[str, str]] = [
    ("BROWSER_MISSING", r"browser[^\n]{0,40}not installed|executable doesn.t exist|"
                        r"please run[^\n]{0,40}playwright install|firefox not installed"),
    ("NETWORK_ERROR", r"err_connection|err_name_not_resolved|err_internet_disconnected|"
                      r"this site can.t be reached|connection refused|unable to connect"),
    ("HTTP_ERROR", r"\b404 not found\b|\b500 internal server error\b|\b502 bad gateway\b"),
    ("AUTH_WALL", r"session (has )?expired|please (log|sign) in again|invalid credentials"),
    ("DEPENDENCY_WARNING", r"command not found|no such file or directory|module not found|"
                           r"dependency[^\n]{0,30}missing"),
    ("INCOMPLETE_NAV", r"^loading\.{3}$|please wait\.{3}|processing request|\A\s*\Z"),
    ("TLS_WARNING", r"your connection is not private|net::err_cert"),
]

_COMPILED = [(name, re.compile(pat, re.IGNORECASE | re.MULTILINE))
             for name, pat in FORBIDDEN_SCREEN_SIGNATURES]


@dataclass
class StateLock:
    """A deterministic proof that the step reached its target state."""
    kind: str          # terminal_regex | text_assert | dom_selector | exit_code | net_idle
    value: str = ""

    def verify(self, text: str, result: ToolResult | None = None) -> tuple[bool, str]:
        if self.kind == "terminal_regex":
            m = re.search(self.value, text, re.IGNORECASE | re.MULTILINE)
            return (True, f"/{self.value}/ matched: {m.group(0)[:80]!r}") if m else \
                   (False, f"/{self.value}/ not found in output")
        if self.kind in ("text_assert", "dom_selector"):
            ok = self.value.lower() in text.lower()
            return (True, f"text present: {self.value!r}") if ok else \
                   (False, f"text absent: {self.value!r}")
        if self.kind == "exit_code":
            code = (result.data or {}).get("exit_code") if result else None
            ok = code == int(self.value or 0)
            return (ok, f"exit_code={code}")
        if self.kind == "net_idle":
            return (False, "network idle is not independently verified")
        return (False, f"unknown lock kind {self.kind}")

    @staticmethod
    def from_dict(d: dict | None) -> "StateLock":
        d = d or {}
        return StateLock(d.get("kind", "text_assert"), str(d.get("value", "")))


class EvidenceGuard:
    def __init__(self, repo: Repo, evidence_dir: Path, settle_seconds: float = 0.0):
        self.repo = repo
        self.dir = Path(evidence_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settle_seconds = settle_seconds
        self._seen_hashes: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ api
    def capture(
        self,
        *,
        job_id: str,
        step_id: int | None,
        step_no: int,
        adapter: ToolAdapter,
        lock: StateLock,
        lock_ok: bool,
        lock_detail: str,
        screen_text: str,
        title: str = "",
        kind: str = "screen",
    ) -> dict:
        """Capture evidence for a verified step, or raise EvidenceRejected."""
        if not lock_ok:
            raise self._reject(job_id, step_id, "LOCK_NOT_VERIFIED",
                               f"step {step_no}: {lock_detail}")
        if self.settle_seconds:
            time.sleep(self.settle_seconds)

        hit = _forbidden(screen_text)
        if hit:
            raise self._reject(job_id, step_id, f"FORBIDDEN_SCREEN:{hit}",
                               _excerpt(screen_text, hit))

        res = adapter.run("screenshot", title=title, kind=kind, transcript=screen_text) \
            if adapter.kind != "browser" else adapter.run("screenshot", title=title)
        if not res.ok:
            raise self._reject(job_id, step_id, "CAPTURE_FAILED", res.message)

        png = (res.data or {}).get("png")
        ext = "png"
        if not isinstance(png, bytes):
            raise self._reject(job_id, step_id, "CAPTURE_FAILED", "screenshot image unavailable")
        digest = hashlib.sha256(png).hexdigest()

        seen = self._seen_hashes.setdefault(job_id, set())
        if digest in seen:
            raise self._reject(job_id, step_id, "DUPLICATE_FRAME",
                               "identical to an earlier capture in this job -- would not "
                               "demonstrate the step")
        seen.add(digest)

        path = self.dir / f"{job_id}" / f"step{step_no:02d}_{digest[:10]}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)

        ev_id = self.repo.add_evidence(
            job_id=job_id, step_id=step_id, file_path=str(path), sha256=digest,
            lock_kind=lock.kind, lock_detail=lock_detail,
            width=int((res.data or {}).get("width") or 0), height=0,
        )
        return {"id": ev_id, "path": str(path), "sha256": digest, "lock_detail": lock_detail}

    def _reject(self, job_id: str, step_id: int | None, reason: str, detail: str) -> EvidenceRejected:
        self.repo.reject_evidence(job_id=job_id, step_id=step_id, reason=reason, detail=detail)
        return EvidenceRejected(reason, detail)


def _forbidden(text: str) -> str | None:
    for name, rx in _COMPILED:
        if rx.search(text or ""):
            return name
    return None


def _excerpt(text: str, name: str, width: int = 160) -> str:
    for nm, rx in _COMPILED:
        if nm == name:
            m = rx.search(text or "")
            if m:
                s = max(0, m.start() - width // 2)
                return text[s:s + width].replace("\n", " ")
    return (text or "")[:width]
