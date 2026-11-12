"""Tool adapter contracts.

The spec's "~45 CLI and GUI security tools behind MCP servers" all reduce to
this interface. Anything implementing it can be registered in the ToolRegistry
and driven by the executor; Playwright, ssh, nmap, snmpwalk, curl, a vendor SDK
etc. become interchangeable adapters.

Deliberate design points:
  * ``run()`` never raises for expected failures. It returns ToolResult(ok=False)
    with a machine-readable ``code`` so the healer can dispatch on it. Raising
    would let a single vendor quirk abort a whole compliance run.
  * Every op carries ``mutating`` metadata. Phase-2 dry runs and read-only
    contexts refuse mutating ops at the adapter boundary, not by convention.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ToolErrorCode:
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    UNKNOWN_MENU = "UNKNOWN_MENU"
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    TEXT_NOT_FOUND = "TEXT_NOT_FOUND"
    SERVICE_DISABLED = "SERVICE_DISABLED"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    AUTH_FAILED = "AUTH_FAILED"
    TIMEOUT = "TIMEOUT"
    NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
    POLICY_REFUSED = "POLICY_REFUSED"
    BROWSER_MISSING = "BROWSER_MISSING"
    UNKNOWN = "UNKNOWN"


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    code: str = ""
    message: str = ""
    latency_ms: int = 0
    raw: Any = None
    text: str = ""          # visible terminal/screen text used by state locks
    meta: dict = field(default_factory=dict)

    @staticmethod
    def success(data: Any = None, text: str = "", meta: dict | None = None) -> "ToolResult":
        return ToolResult(ok=True, data=data, text=text, meta=meta or {})

    @staticmethod
    def failure(code: str, message: str, text: str = "", meta: dict | None = None) -> "ToolResult":
        return ToolResult(ok=False, code=code, message=message, text=text, meta=meta or {})


@runtime_checkable
class ToolAdapter(Protocol):
    name: str
    kind: str                     # 'cli' | 'browser' | 'net' | 'file' | 'mcp'
    read_only: bool

    def available(self) -> tuple[bool, str]: ...

    def run(self, op: str, /, **args: Any) -> ToolResult: ...


class BaseAdapter:
    """Shared plumbing: read-only enforcement, timing, op dispatch."""

    name = "base"
    kind = "mcp"
    read_only = False
    mutating_ops: frozenset[str] = frozenset()

    def available(self) -> tuple[bool, str]:
        return True, ""

    def run(self, op: str, /, **args: Any) -> ToolResult:
        started = time.perf_counter()
        if self.read_only and op in self.mutating_ops:
            return ToolResult.failure(
                ToolErrorCode.POLICY_REFUSED,
                f"adapter {self.name} is in read-only mode; op '{op}' is mutating",
            )
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return ToolResult.failure(
                ToolErrorCode.UNKNOWN, f"adapter {self.name} has no op '{op}'"
            )
        try:
            res = handler(**args)
        except TypeError as exc:
            return ToolResult.failure(ToolErrorCode.UNKNOWN, f"bad arguments for {op}: {exc}")
        if not isinstance(res, ToolResult):
            res = ToolResult.success(res)
        res.latency_ms = int((time.perf_counter() - started) * 1000)
        return res
