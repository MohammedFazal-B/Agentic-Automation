"""Registry of available tool adapters ("the MCP pool").

Adapters are looked up by the ``tool`` field of a plan step. If a plan
references a tool that is not registered, the executor raises a *plan* defect
(correctable by healing) rather than crashing — that distinction matters for
auditability.
"""
from __future__ import annotations

from .base import BaseAdapter, ToolAdapter, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {}

    def register(self, adapter: ToolAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ToolAdapter | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def health(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, ad in self._adapters.items():
            ok, why = ad.available()
            out[name] = "ready" if ok else f"unavailable: {why}"
        return out

    def require(self, name: str) -> ToolAdapter:
        ad = self.get(name)
        if ad is None:
            raise KeyError(name)
        return ad

    def set_read_only(self, flag: bool) -> None:
        for ad in self._adapters.values():
            if isinstance(ad, BaseAdapter):
                ad.read_only = flag
