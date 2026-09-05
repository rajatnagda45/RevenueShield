"""TRAI DND (Do Not Disturb) registry lookup, pluggable.

Production would call a DLT/DND scrubbing provider; the default implementation is a static list
from settings so the behaviour is testable and the interface is fixed.
"""
from __future__ import annotations

from typing import Optional, Protocol, Set

from app.core.config import settings


class DndRegistry(Protocol):
    def is_registered(self, phone: Optional[str]) -> bool: ...


def _normalise(phone: Optional[str]) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


class StaticDndRegistry:
    """DND numbers from `DND_REGISTRY_NUMBERS` (comma separated)."""

    def __init__(self, numbers: Optional[Set[str]] = None):
        raw = numbers if numbers is not None else {n.strip() for n in (settings.DND_REGISTRY_NUMBERS or "").split(",") if n.strip()}
        self._numbers = {_normalise(n) for n in raw if _normalise(n)}

    def is_registered(self, phone: Optional[str]) -> bool:
        key = _normalise(phone)
        return bool(key) and key in self._numbers


_registry: Optional[DndRegistry] = None


def get_dnd_registry() -> DndRegistry:
    global _registry
    if _registry is None:
        _registry = StaticDndRegistry()
    return _registry


def set_dnd_registry(registry: Optional[DndRegistry]) -> None:
    """Swap the registry implementation (tests, or a real scrubbing provider)."""
    global _registry
    _registry = registry
