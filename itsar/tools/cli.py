"""Production CLI adapter (SSH / console) implementing the same contract as the mock.

Credentials are resolved at call time from the credential store by reference --
they are never held in the DB, the plan, or the log.
"""
from __future__ import annotations

import shutil
import socket
import subprocess

from .base import BaseAdapter, ToolErrorCode, ToolResult


class SshCliAdapter(BaseAdapter):
    name = "cli_ssh"
    kind = "cli"
    mutating_ops = frozenset({"config"})

    def __init__(self, host: str, credential_ref: str, credentials: dict, timeout: int = 20):
        self.host = host
        self.credential_ref = credential_ref
        self.credentials = credentials      # {"username":..., "password":..., ...}
        self.timeout = timeout
        self._client = None

    # ------------------------------------------------------------ session
    def available(self) -> tuple[bool, str]:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            return False, "paramiko not installed (pip install paramiko)"
        return True, ""

    def _connect(self):
        if self._client is not None:
            return self._client
        import paramiko

        cred = self.credentials.get(self.credential_ref) or self.credentials
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=int(cred.get("port", 22)),
            username=cred["username"],
            password=cred.get("password"),
            key_filename=cred.get("key_file"),
            timeout=self.timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        self._client = client
        return client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _exec(self, command: str) -> ToolResult:
        try:
            client = self._connect()
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.NETWORK_UNREACHABLE, f"ssh connect failed: {exc}")
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=self.timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        except Exception as exc:
            return ToolResult.failure(ToolErrorCode.TIMEOUT, f"ssh exec failed: {exc}")
        text = (out + ("\n" + err if err.strip() else "")).strip()
        if rc != 0 or "% Invalid input" in text or "% Invalid" in text:
            return ToolResult.failure(ToolErrorCode.UNKNOWN_COMMAND, text, text=text)
        return ToolResult.success({"exit_code": rc}, text=text)

    # ---------------------------------------------------------------- ops
    def op_probe_port(self, port: int, host: str | None = None) -> ToolResult:
        target = host or self.host
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            ok = sock.connect_ex((target, int(port))) == 0
        finally:
            sock.close()
        if not ok:
            return ToolResult.failure(
                ToolErrorCode.SERVICE_DISABLED, f"tcp/{port} closed on {target}",
                text=f"{port}/tcp closed",
            )
        return ToolResult.success({"port": port, "state": "open"},
                                 text=f"{port}/tcp open")

    def op_exec(self, command: str) -> ToolResult:
        return self._exec(command)

    def op_config(self, command: str, commit_wrapper: str | None = None) -> ToolResult:
        """Vendor-neutral config write. Override the wrapper per platform."""
        wrapped = commit_wrapper.format(cmd=command) if commit_wrapper else command
        return self._exec(wrapped)

    def op_screenshot(self, title: str = "", kind: str = "terminal", transcript: str = "") -> ToolResult:
        """Terminal evidence: the transcript IS the artefact (rendered to PNG)."""
        from .png import render_terminal

        png = render_terminal(transcript.splitlines() or ["(empty)"], title=title)
        return ToolResult.success({"png": png}, text=transcript)


class NmapAdapter(BaseAdapter):
    """Port/protocol precondition probing via nmap, when installed."""

    name = "nmap"
    kind = "net"

    def __init__(self, host: str):
        self.host = host

    def available(self) -> tuple[bool, str]:
        return (True, "") if shutil.which("nmap") else (False, "nmap binary not found")

    def op_probe_port(self, port: int) -> ToolResult:
        proc = subprocess.run(
            ["nmap", "-Pn", "-p", str(port), "--host-timeout", "15s", self.host],
            capture_output=True, text=True, timeout=40,
        )
        out = proc.stdout
        if "open" in out and "closed" not in out.split(str(port), 1)[-1][:20]:
            return ToolResult.success({"port": port, "state": "open"}, text=out)
        return ToolResult.failure(ToolErrorCode.SERVICE_DISABLED, f"port {port} not open", text=out)
