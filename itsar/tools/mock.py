"""A simulated DUT + adapters, so the full pipeline is exercisable without hardware.

This is NOT a stub that always returns success. It deliberately behaves like a
real device:
  * ports can be closed, services disabled, features absent from firmware
  * CLI rejects unknown commands
  * the web UI has real navigation depth; a label that moved between firmware
    builds produces SELECTOR_NOT_FOUND with candidate suggestions -- which is
    exactly the failure the self-healing loop exists for
  * GUI-only defaults differ between firmware builds

Swapping in production adapters (itsar/tools/cli.py, itsar/tools/browser.py)
requires no change to the executor, planner, or DB layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import BaseAdapter, ToolErrorCode, ToolResult
from .png import render_screen, render_terminal


# ----------------------------------------------------------------- profiles
OLD_FIRMWARE = {
    "firmware": "4.2.1",
    "hostname": "AC-RTR-9200",
    "ports": {22: True, 23: False, 443: True, 161: True},
    "ssh_version": "2.0",
    "capabilities": {"password_policy", "session_timeout", "remote_syslog", "mfa_totp", "ssh"},
    "ui_labels": {
        "password_policy_menu": "Password Policy",
        "nav_users": "User Management",
    },
    "syslog_level": "informational",
    "password_policy": {"min_length": 8, "complexity": "low", "history": 3},
    "config": {
        "session": {"idle_timeout_minutes": 30},
        "ssh": {"server": False, "version": "2.0"},
        "telnet": {"server": False},
        "logging": {"remote_host": "", "level": "informational"},
        "password_policy": {"min_length": 8, "complexity": "low", "history": 3},
    },
}

NEW_FIRMWARE = {
    "firmware": "5.0.0",
    "hostname": "AC-RTR-9500",
    # SSH is disabled out of the box on this build (precondition remediation case)
    "ports": {22: False, 23: True, 443: True, 161: True},
    "ssh_version": "2.0",
    # MFA was dropped from this firmware line
    "capabilities": {"password_policy", "session_timeout", "remote_syslog", "ssh"},
    "ui_labels": {
        # The label AND the menu depth both changed in 5.0.0
        "password_policy_menu": "Password & Complexity Policy",
        "nav_users": "Local Users",
    },
    "syslog_level": "notice",
    "password_policy": {"min_length": 8, "complexity": "low", "history": 3},
    "config": {
        "session": {"idle_timeout_minutes": 60},
        "ssh": {"server": False, "version": "2.0"},
        "telnet": {"server": True},
        "logging": {"remote_host": "", "level": "notice"},
        "password_policy": {"min_length": 8, "complexity": "low", "history": 3},
    },
}


@dataclass
class MockDut:
    profile: dict = field(default_factory=lambda: dict(OLD_FIRMWARE))
    config: dict = field(default_factory=dict)
    current_url: str = "/dashboard"
    _pending: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config = {k: dict(v) if isinstance(v, dict) else v
                       for k, v in self.profile["config"].items()}

    # ------------------------------------------------------------- plumbing
    @property
    def password_policy_label(self) -> str:
        return self.profile["ui_labels"]["password_policy_menu"]

    def _users_path(self) -> str:
        return "/admin/" + self.profile["ui_labels"]["nav_users"].lower().replace(" ", "-")

    def page(self, url: str) -> dict[str, Any] | None:
        p = self._users_path()
        pages: dict[str, dict] = {
            "/dashboard": {
                "title": "System Dashboard",
                "text": ["SYSTEM DASHBOARD", f"MODEL: {self.profile['hostname']}",
                         f"FIRMWARE: {self.profile['firmware']}", "STATUS: OPERATIONAL"],
                "elements": {
                    "Administration": "/admin",
                    "Monitoring": "/monitoring",
                    "Security": "/security",
                    "Logout": "/logout",
                },
            },
            "/admin": {
                "title": "Administration",
                "text": ["ADMINISTRATION"],
                "elements": {
                    self.profile["ui_labels"]["nav_users"]: p,
                    "Logging": "/admin/logging",
                    "Back": "/dashboard",
                },
            },
            p: {
                "title": self.profile["ui_labels"]["nav_users"],
                "text": ["USER MANAGEMENT"],
                "elements": {
                    self.password_policy_label: p + "/password-policy",
                    "Back": "/admin",
                },
            },
            p + "/password-policy": {
                "title": self.password_policy_label,
                "text": [
                    self.password_policy_label.upper(),
                    f"MINIMUM PASSWORD LENGTH: {self.config['password_policy']['min_length']}",
                    f"COMPLEXITY: {self.config['password_policy']['complexity']}",
                    f"PASSWORD HISTORY: {self.config['password_policy']['history']}",
                ],
                "elements": {"Save": "ACTION:save", "Cancel": "ACTION:cancel"},
            },
            "/admin/logging": {
                "title": "Logging",
                "text": ["LOGGING", f"REMOTE SYSLOG HOST: {self.config['logging']['remote_host'] or '(none)'}",
                         f"LOG LEVEL: {self.config['logging']['level']}"],
                "elements": {"Save": "ACTION:save", "Back": "/admin"},
            },
            "/security": {
                "title": "Security",
                "text": ["SECURITY"],
                "elements": {
                    "Authentication": "/security/auth",
                    "Firewall": "/security/firewall",
                    "Back": "/dashboard",
                },
            },
            "/security/auth": {
                "title": "Authentication",
                "text": ["AUTHENTICATION"],
                "elements": {
                    "Multi-Factor Authentication": "/security/auth/mfa"
                    if "mfa_totp" in self.profile["capabilities"] else "MISSING:mfa",
                    "Session Settings": "/security/auth/session",
                    "Back": "/security",
                },
            },
            "/security/auth/session": {
                "title": "Session Settings",
                "text": ["SESSION SETTINGS",
                         f"IDLE TIMEOUT (MINUTES): {self.config['session']['idle_timeout_minutes']}"],
                "elements": {"Save": "ACTION:save", "Back": "/security/auth"},
            },
            "/security/auth/mfa": {
                "title": "Multi-Factor Authentication",
                "text": ["MULTI-FACTOR AUTHENTICATION", "STATUS: DISABLED",
                         "TOTP SUPPORT: ENABLED"],
                "elements": {"Enable": "ACTION:enable", "Back": "/security/auth"},
            },
        }
        return pages.get(url)

    # -------------------------------------------------------------- CLI
    def cli(self, command: str) -> ToolResult:
        cfg = self.config
        cmd = " ".join(command.strip().split())
        lower = cmd.lower()
        responses: dict[str, str] = {
            "show version": f"{self.profile['hostname']} Firmware {self.profile['firmware']}\n"
                            f"Boot image: lx-os-{self.profile['firmware']}.bin",
            "show ssh": f"SSH server: {'enabled' if cfg['ssh']['server'] else 'disabled'}\n"
                        f"SSH protocol version: {cfg['ssh']['version']}",
            "show ip telnet": f"Telnet server: {'enabled' if cfg['telnet']['server'] else 'disabled'}",
            "show session-config": f"Session idle-timeout: {cfg['session']['idle_timeout_minutes']} minutes\n"
                                   "Max concurrent sessions: 4",
            "show password-policy": f"min-length {cfg['password_policy']['min_length']}\n"
                                    f"complexity {cfg['password_policy']['complexity']}\n"
                                    f"history {cfg['password_policy']['history']}",
            "show logging-config": f"remote-syslog-host {cfg['logging']['remote_host'] or 'none'}\n"
                                   f"log-level {cfg['logging']['level']}",
            "show running-config": self._running_config(),
            "show interface mgmt0": "mgmt0 is up\n  ip address 10.10.20.11/24\n  mtu 1500",
            "show features": "capabilities: " + ", ".join(sorted(self.profile["capabilities"])),
        }
        if lower in responses:
            return ToolResult.success(responses[lower], text=responses[lower])
        return ToolResult.failure(
            ToolErrorCode.UNKNOWN_COMMAND,
            f"% Invalid input detected at '^' marker: {cmd}",
            text="% Invalid input",
        )

    def _running_config(self) -> str:
        c = self.config
        return "\n".join([
            f"hostname {self.profile['hostname']}",
            f"ssh server {'enable' if c['ssh']['server'] else 'disable'}",
            f"ssh protocol-version {c['ssh']['version']}",
            f"telnet server {'enable' if c['telnet']['server'] else 'disable'}",
            f"session idle-timeout {c['session']['idle_timeout_minutes']}",
            f"password-policy min-length {c['password_policy']['min_length']}",
            f"password-policy complexity {c['password_policy']['complexity']}",
            f"password-policy history {c['password_policy']['history']}",
            f"logging remote-host {c['logging']['remote_host'] or 'none'}",
            f"logging level {c['logging']['level']}",
            "end",
        ])

    def cli_config(self, command: str) -> ToolResult:
        """Mutating CLI. Only accepts the commands a device would accept."""
        lower = " ".join(command.strip().split()).lower()
        applied = None
        if lower.startswith("ssh server enable"):
            self.config["ssh"]["server"] = True
            applied = "ssh server enable"
        elif lower.startswith("ssh protocol-version 2"):
            self.config["ssh"]["version"] = "2.0"
            applied = "ssh protocol-version 2.0"
        elif lower.startswith("telnet server disable"):
            self.config["telnet"]["server"] = False
            applied = "telnet server disable"
        elif lower.startswith("session idle-timeout"):
            try:
                self.config["session"]["idle_timeout_minutes"] = int(lower.split()[-1])
                applied = lower
            except ValueError:
                pass
        elif lower.startswith("logging remote-host"):
            self.config["logging"]["remote_host"] = lower.split()[-1]
            applied = lower
        elif lower.startswith("logging level"):
            self.config["logging"]["level"] = lower.split()[-1]
            applied = lower
        if applied:
            return ToolResult.success(
                {"applied": applied}, text=f"Configuration committed.\n{applied}"
            )
        return ToolResult.failure(
            ToolErrorCode.UNKNOWN_COMMAND, f"% Invalid configuration command: {command}"
        )

    # --------------------------------------------------------------- GUI
    def click(self, label: str) -> ToolResult:
        page = self.page(self.current_url)
        if not page:
            return ToolResult.failure(ToolErrorCode.SELECTOR_NOT_FOUND,
                                      f"no page at {self.current_url}")
        elements: dict[str, str] = page["elements"]
        if label not in elements:
            # Give the healer the same affordance a real DOM inspection would.
            candidates = [k for k in elements if _fuzzy(k, label)]
            return ToolResult.failure(
                ToolErrorCode.SELECTOR_NOT_FOUND,
                f"element '{label}' not found on {self.current_url}",
                text=" | ".join(elements),
                meta={"candidates": candidates, "available": list(elements)},
            )
        target = elements[label]
        if target.startswith("MISSING:"):
            return ToolResult.failure(
                ToolErrorCode.UNSUPPORTED_FEATURE,
                f"'{label}' is not present in firmware {self.profile['firmware']}",
                meta={"capability": target.split(":", 1)[1]},
            )
        if target.startswith("ACTION:"):
            return self.action(target.split(":", 1)[1])
        self.current_url = target
        return ToolResult.success({"url": target}, text=self.read_text().text)

    def action(self, name: str) -> ToolResult:
        if name == "save":
            if "password" in self.current_url:
                self.config["password_policy"] = {
                    "min_length": max(self.config["password_policy"]["min_length"],
                                      int(self._pending.get("min_length", 0)) or 0)
                    if self._pending else self.config["password_policy"]["min_length"],
                    "complexity": self._pending.get("complexity",
                                                    self.config["password_policy"]["complexity"]),
                    "history": self._pending.get("history",
                                                 self.config["password_policy"]["history"]),
                }
            return ToolResult.success(
                {"saved": True},
                text=self.read_text().text + "\n\nCONFIGURATION SAVED SUCCESSFULLY",
            )
        if name == "enable":
            return ToolResult.success({"enabled": True}, text="MFA ENABLED")
        return ToolResult.failure(ToolErrorCode.UNKNOWN, f"unknown action {name}")


    def set_field(self, field_name: str, value: Any) -> ToolResult:
        if not isinstance(self._pending, dict):
            self._pending = {}
        self._pending[field_name] = value
        return ToolResult.success({"field": field_name, "value": value},
                                  text=f"{field_name} set to {value}")

    def read_text(self) -> ToolResult:
        page = self.page(self.current_url)
        if not page:
            return ToolResult.failure(ToolErrorCode.SELECTOR_NOT_FOUND,
                                      f"no page at {self.current_url}")
        return ToolResult.success({"title": page["title"], "url": self.current_url},
                                 text="\n".join(page["text"]))

    def goto(self, url: str) -> ToolResult:
        if not self.page(url):
            return ToolResult.failure(ToolErrorCode.SELECTOR_NOT_FOUND, f"404 {url}",
                                     text="404 NOT FOUND")
        self.current_url = url
        return ToolResult.success({"url": url}, text=self.read_text().text)

    def discover_ui_path(self, keyword: str) -> ToolResult:
        """Non-destructive BFS over the menu tree (the Phase-2 'dry run')."""
        start = "/dashboard"
        queue: list[tuple[str, list[str]]] = [(start, [])]
        seen = {start}
        while queue:
            url, path = queue.pop(0)
            page = self.page(url)
            if not page:
                continue
            if keyword.lower() in (page["title"] + " " + " ".join(page["elements"])).lower():
                return ToolResult.success({"url": url, "click_path": path,
                                           "title": page["title"]},
                                          text=page["title"])
            for label, target in page["elements"].items():
                if target.startswith("/") and target not in seen:
                    seen.add(target)
                    queue.append((target, path + [label]))
        return ToolResult.failure(ToolErrorCode.TEXT_NOT_FOUND, f"no page matching '{keyword}'")


def _fuzzy(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if a in b or b in a:
        return True
    aw, bw = set(a.split()), set(b.split())
    return len(aw & bw) >= 1 and abs(len(aw) - len(bw)) <= 3


# ---------------------------------------------------------------- adapters
class MockCliAdapter(BaseAdapter):
    name = "cli"
    kind = "cli"
    mutating_ops = frozenset({"config"})

    def __init__(self, dut: MockDut):
        self.dut = dut

    def op_probe_port(self, port: int) -> ToolResult:
        open_ = self.dut.profile["ports"].get(port, False)
        if not open_:
            return ToolResult.failure(
                ToolErrorCode.SERVICE_DISABLED,
                f"tcp/{port} is closed on {self.dut.profile['hostname']}",
                text=f"nmap: {port}/tcp closed",
            )
        return ToolResult.success({"port": port, "state": "open"},
                                  text=f"PORT {port}/TCP OPEN")

    def op_exec(self, command: str) -> ToolResult:
        # a closed management port means no CLI at all
        if not self.dut.profile["ports"].get(22, False):
            return ToolResult.failure(ToolErrorCode.NETWORK_UNREACHABLE,
                                      "ssh: connect to host port 22: Connection refused")
        return self.dut.cli(command)

    def op_config(self, command: str) -> ToolResult:
        if not self.dut.profile["ports"].get(22, False):
            return ToolResult.failure(ToolErrorCode.NETWORK_UNREACHABLE,
                                      "ssh: connect to host port 22: Connection refused")
        return self.dut.cli_config(command)

    def op_screenshot(self, title: str = "", kind: str = "terminal",
                      transcript: str = "") -> ToolResult:
        png = render_terminal(transcript.splitlines() or ["(empty)"], title=title)
        return ToolResult.success({"png": png, "width": 900}, text=transcript)

    def op_enable_ssh(self) -> ToolResult:
        """Console/local remediation: bring SSH back without using SSH."""
        self.dut.profile["ports"][22] = True
        self.dut.config["ssh"]["server"] = True
        return ToolResult.success({"ssh": "enabled"},
                                  text="ssh server enable\nConfiguration committed.")


class MockBrowserAdapter(BaseAdapter):
    name = "browser"
    kind = "browser"
    mutating_ops = frozenset({"click", "set_field", "action"})

    def __init__(self, dut: MockDut):
        self.dut = dut

    def available(self) -> tuple[bool, str]:
        return True, ""

    def op_goto(self, url: str) -> ToolResult:
        return self.dut.goto(url)

    def op_click(self, label: str) -> ToolResult:
        return self.dut.click(label)

    def op_read_text(self) -> ToolResult:
        return self.dut.read_text()

    def op_discover(self, keyword: str) -> ToolResult:
        return self.dut.discover_ui_path(keyword)

    def op_set_field(self, field: str, value: Any) -> ToolResult:
        return self.dut.set_field(field, value)

    def op_screenshot(self, title: str = "", kind: str = "screen") -> ToolResult:
        page = self.dut.page(self.dut.current_url)
        text = "\n".join(page["text"]) if page else ""
        if kind == "terminal":
            png = render_terminal(text.splitlines(), title=title)
        else:
            png = render_screen(title or (page["title"] if page else ""),
                                self.dut.current_url, text.splitlines())
        return ToolResult.success({"png": png, "width": 1000},
                                  text=text, meta={"url": self.dut.current_url})
