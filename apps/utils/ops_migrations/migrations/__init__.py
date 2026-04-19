"""Migration modules.

Each file named `_NNNN_*.py` defines one `Migration` subclass and calls
`@register` on it. Importing this package side-effectfully populates the
registry — `registry.all_migrations()` imports us lazily on first use.
"""
from . import _0001_namespace_catalog_volumes  # noqa: F401
