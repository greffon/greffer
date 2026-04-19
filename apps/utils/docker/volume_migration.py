"""Compatibility shim — the logic moved to the ops-migrations framework.

This module keeps a thin `run(data_root=None)` callable so any external code
(none found in-tree beyond the old views.py import that's now removed)
still works through one deprecation cycle. New callers should prefer

    python manage.py apply_ops_migrations

or, programmatically,

    from apps.utils.ops_migrations import runner
    runner.apply_pending(data_root)

This shim will be removed in a follow-up release.
"""
from __future__ import annotations

import warnings

from apps.utils.ops_migrations.migrations._0001_namespace_catalog_volumes import (
    NamespaceCatalogVolumes,
)

SENTINEL_NAME = ".volumes-migrated"  # preserved for back-compat imports


def run(data_root: str | None = None) -> dict:
    """Deprecated entry point. Delegates to the 0001 migration's run()."""
    warnings.warn(
        "apps.utils.docker.volume_migration.run is deprecated. Use the "
        "`apply_ops_migrations` management command, or call "
        "`apps.utils.ops_migrations.runner.apply_pending(data_root)`.",
        DeprecationWarning,
        stacklevel=2,
    )
    import os
    data_root = data_root or os.getenv("GREFFON_PATH", "/data")
    return NamespaceCatalogVolumes().run(data_root)
