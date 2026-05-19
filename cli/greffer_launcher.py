"""Top-level launcher for the PyInstaller-frozen ``greffer`` binary.

PyInstaller runs the entrypoint script as ``__main__``, NOT as
``greffer_cli.main`` — which means the relative imports at the top of
``greffer_cli/main.py`` (``from . import compose, doctor, …``) have
no parent package and raise ``ImportError`` before Typer even
constructs its command tree. The `poetry run greffer` path doesn't
hit this because Poetry's generated shim imports the package
properly.

This launcher does the import-as-package step explicitly, so the
frozen binary behaves the same as the Poetry shim. Keep it minimal:
no logic here, just the entry call.

Referenced from ``greffer.spec`` as the analyzed script.
"""

from greffer_cli.main import app


if __name__ == "__main__":
    app()
