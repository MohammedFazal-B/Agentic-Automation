"""Playwright MCP browser adapter.

Implements directive 3 exactly:
  * Google Chrome is the mandatory default browser.
  * If a step declares ``requires_firefox`` (some security tools only ship a
    Firefox driver), Firefox is installed on the fly -- the step is not skipped
    and not failed.
  * A missing browser is reported as BROWSER_MISSING, and the executor's
    remediation step installs it before retrying.

Transport: JSON-RPC 2.0 over stdio to an MCP server. Point ``command`` at your
Playwright MCP server (e.g. ``npx @playwright/mcp@latest``). ``AvailablePage``
is abstracted so the same adapter can drive the MCP server or an in-process
Playwright install for tests.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from .base import BaseAdapter, ToolErrorCode, ToolResult


class StdioMcpClient:
    """Minimal MCP stdio client: initialize, then tools/call, one at a time."""

    def __init__(self, command: list[str], request_timeout: int = 60):
        self.command = command
        self.request_timeout = request_timeout
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._tools_cache: set[str] = set()

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "itsar-agent", "version": "1.0"},
        })
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            self._proc = None

    def _send(self, payload: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        assert self._proc and self._proc.stdout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed the stream")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self._next_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "mcp error"))
                return msg.get("result", {})

    def tool_names(self) -> set[str]:
        if not self._tools_cache:
            res = self._rpc("tools/list", {})
            self._tools_cache = {t["name"] for t in res.get("tools", [])}
        return self._tools_cache

    def call(self, tool: str, arguments: dict) -> Any:
        res = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        return res


class PlaywrightMcpBrowserAdapter(BaseAdapter):
    name = "browser"
    kind = "browser"
    mutating_ops = frozenset({"click", "set_field", "action", "press"})

    def __init__(
        self,
        mcp_command: list[str] | None = None,
        chrome_path: str = "/usr/bin/google-chrome",
        firefox_path: str = "/usr/bin/firefox",
        allow_firefox: bool = True,
        headless: bool = True,
    ):
        self.mcp_command = mcp_command or ["npx", "@playwright/mcp@latest", "--headless"]
        self.chrome_path = chrome_path
        self.firefox_path = firefox_path
        self.allow_firefox = allow_firefox
        self.headless = headless
        self.client: StdioMcpClient | None = None
        self.browser_kind = "chrome"
        self._pending_install: str | None = None

    # ------------------------------------------------------------ lifecycle
    def available(self) -> tuple[bool, str]:
        if shutil.which(self.mcp_command[0]) is None:
            return False, f"{self.mcp_command[0]} not on PATH"
        if not (shutil.which(self.chrome_path) or shutil.which("google-chrome")):
            return False, "google-chrome not installed (run: playwright install chrome)"
        return True, ""

    def install_browser(self, kind: str = "firefox") -> ToolResult:
        """On-the-fly install so a Firefox-only tool never blocks a run."""
        if kind == "firefox" and not self.allow_firefox:
            return ToolResult.failure(
                ToolErrorCode.POLICY_REFUSED, "firefox fallback disabled by policy"
            )
        try:
            proc = subprocess.run(
                ["playwright", "install", kind], capture_output=True, text=True, timeout=600
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return ToolResult.failure(ToolErrorCode.BROWSER_MISSING, f"install {kind} failed: {exc}")
        ok = proc.returncode == 0
        return (ToolResult.success({"installed": kind}, text=proc.stdout) if ok
                else ToolResult.failure(ToolErrorCode.BROWSER_MISSING, proc.stderr))

    def _ensure_session(self, browser_kind: str = "chrome") -> ToolResult:
        if self.client is not None and self.browser_kind == browser_kind:
            return ToolResult.success()
        exe = self.chrome_path if browser_kind == "chrome" else self.firefox_path
        if not (shutil.which(exe) or (browser_kind == "firefox" and shutil.which("firefox"))):
            install = self.install_browser(browser_kind)
            if not install.ok:
                return install
        try:
            self.client = StdioMcpClient(self.mcp_command)
            self.client.start()
            self.browser_kind = browser_kind
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.BROWSER_MISSING, f"mcp start failed: {exc}")
        return ToolResult.success()

    # ---------------------------------------------------------------- ops
    def op_goto(self, url: str, requires_firefox: bool = False) -> ToolResult:
        sess = self._ensure_session("firefox" if requires_firefox else "chrome")
        if not sess.ok:
            return sess
        try:
            self.client.call("browser_navigate", {"url": url})  # type: ignore[union-attr]
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.TIMEOUT, str(exc))
        return self._snapshot()

    def op_click(self, label: str, selector: str | None = None) -> ToolResult:
        try:
            self.client.call(  # type: ignore[union-attr]
                "browser_click",
                {"element": label, "ref": label} if not selector
                else {"element": label, "selector": selector},
            )
        except Exception as exc:
            msg = str(exc)
            code = (ToolErrorCode.SELECTOR_NOT_FOUND
                    if "not found" in msg.lower() or "resolved to" in msg.lower()
                    else ToolErrorCode.UNKNOWN)
            return ToolResult.failure(code, msg)
        return self._snapshot()

    def op_set_field(self, field: str, value: Any, selector: str | None = None) -> ToolResult:
        try:
            self.client.call(  # type: ignore[union-attr]
                "browser_fill_form",
                {"fields": [{"name": field, "ref": selector or field, "type": "textbox",
                             "value": str(value)}]},
            )
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.SELECTOR_NOT_FOUND, str(exc))
        return self._snapshot()

    def op_read_text(self) -> ToolResult:
        try:
            res = self.client.call("browser_snapshot", {})  # type: ignore[union-attr]
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.TIMEOUT, str(exc))
        return ToolResult.success(res, text=_extract_text(res))

    def op_discover(self, keyword: str) -> ToolResult:
        """Phase-2 dry run: read the accessibility tree, find the nav path, click nothing."""
        snap = self.op_read_text()
        if not snap.ok:
            return snap
        res = self.client.call("browser_find", {"query": keyword})  # type: ignore[union-attr]
        return ToolResult.success({"keyword": keyword, "matches": res}, text=snap.text)

    def op_screenshot(self, title: str = "", full_page: bool = True) -> ToolResult:
        """Returns real PNG bytes from Chrome. Never called unless the lock passed."""
        try:
            res = self.client.call(  # type: ignore[union-attr]
                "browser_take_screenshot", {"fullPage": full_page, "type": "png"},
            )
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.TIMEOUT, str(exc))
        return ToolResult.success({"mcp_result": res}, text=_extract_text(res))

    def _snapshot(self) -> ToolResult:
        return self.op_read_text()

    def close(self) -> None:
        if self.client:
            self.client.stop()
            self.client = None


def _extract_text(result: Any) -> str:
    if isinstance(result, dict):
        chunks = []
        for c in result.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "text":
                chunks.append(c.get("text", ""))
        return "\n".join(chunks) if chunks else json.dumps(result)[:4000]
    return str(result)
