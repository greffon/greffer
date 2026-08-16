"""The two places the greffer version lives must agree.

``app/__init__.py`` carries the constant the register report stamps onto the
manager (which the per-greffon ``min_greffer_version`` gate then compares
against), and ``pyproject.toml`` carries the one the image is built and tagged
from. Its comment says "keep in sync with pyproject.toml" and nothing enforced
it, so a bump applied to one and not the other would ship an image whose tag
disagrees with the version it reports about itself -- and the updater resolves
targets by tag.
"""
from __future__ import annotations

import pathlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - CI pins 3.11, this is for older locals
    import tomli as tomllib

from app import __version__


def test_the_package_and_module_versions_agree() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / 'pyproject.toml').read_text())
    packaged = data['tool']['poetry']['version']
    assert packaged == __version__, (
        f'pyproject.toml says {packaged} and app/__init__.py says '
        f'{__version__}. The image is tagged from the first and the greffer '
        f'reports the second, so these disagreeing means an update resolves a '
        f'tag whose contents describe themselves differently.'
    )
