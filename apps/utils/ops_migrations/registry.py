"""Migration registry — each concrete `Migration` subclass registers itself.

Usage:

    from apps.utils.ops_migrations.registry import register
    from apps.utils.ops_migrations.base import Migration

    @register
    class MyMig(Migration):
        id = "0002_do_thing"
        ...

The migrations/ subpackage's __init__.py imports every `_NNNN_*.py`, which
triggers the @register decorator at import time and populates `_REGISTRY`.
"""
from __future__ import annotations

from .base import DuplicateMigrationId, Migration

_REGISTRY: dict[str, type[Migration]] = {}


def register(cls: type[Migration]) -> type[Migration]:
    """Decorator + callable. Registers a Migration subclass by its id."""
    if not issubclass(cls, Migration):
        raise TypeError(f"register() expected a Migration subclass, got {cls!r}")
    if not cls.id:
        raise ValueError(f"{cls.__name__} has no id set")
    if cls.id in _REGISTRY:
        existing = _REGISTRY[cls.id]
        raise DuplicateMigrationId(
            f"migration id {cls.id!r} already registered by "
            f"{existing.__module__}.{existing.__name__}; "
            f"second registration from {cls.__module__}.{cls.__name__}"
        )
    _REGISTRY[cls.id] = cls
    return cls


def all_migrations() -> list[Migration]:
    """Return instances of every registered migration, sorted by id."""
    # Ensure every migration module is imported so decorators have fired.
    from . import migrations  # noqa: F401 — import-for-side-effect
    return [cls() for _id, cls in sorted(_REGISTRY.items())]


def reset_for_tests() -> None:
    """Clear the registry. Only call from test setup/teardown."""
    _REGISTRY.clear()
