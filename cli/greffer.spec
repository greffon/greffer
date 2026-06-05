# PyInstaller spec for the `greffer` CLI binary.
#
# Build (locally or on CI):
#     poetry run pyinstaller --clean --noconfirm greffer.spec
#
# The output binary lands at `dist/greffer` and is the single-file
# artifact published to GitHub Releases by .github/workflows/cli-release.yml.
#
# Bundled non-Python data:
#   - greffer_cli/templates/compose.yml — the docker-compose template
#   - greffer_cli/IMAGE_TAG             — pinned image tag
# Both live INSIDE the package; without --collect-data the PyInstaller
# bundle would drop them (mirrors the wheel-bundling bug fixed in
# 278d326) and operators would hit the "latest" fallback + missing
# template at first run.

# noqa: F821 — PyInstaller injects `Analysis`, `PYZ`, `EXE`, etc.
# into the spec namespace; pyflakes flags them as undefined.
#
# Matches the PyInstaller 6.x spec layout (drops the 5.x-era
# `block_cipher` / `cipher=` / `a.zipfiles` / `win_no_prefer_redirects`
# / `win_private_assemblies` args, all removed or no-ops in 6.x). The
# current pyinstaller pin in pyproject.toml is ^6.5 so we don't need
# the 5-compat surface.


a = Analysis(
    # Launcher script — NOT greffer_cli/main.py. PyInstaller runs the
    # analyzed script as `__main__`, which would break the relative
    # imports at the top of main.py. The launcher imports the package
    # properly, then calls the Typer app. See greffer_launcher.py.
    ['greffer_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('greffer_cli/templates/compose.yml', 'greffer_cli/templates'),
        ('greffer_cli/IMAGE_TAG', 'greffer_cli'),
    ],
    hiddenimports=[
        # Typer pulls click; click 8.1 ships extra subpackages that
        # PyInstaller's analyzer sometimes misses on stripped-down
        # CI images. List them explicitly so the bundle never breaks
        # on a corner of the runner image.
        'click.core',
        'click.types',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ONEDIR build (EXE with exclude_binaries=True + a COLLECT step), NOT
# onefile. Onefile re-extracts the entire ~20MB bundle to a fresh
# /tmp/_MEIxxxx directory on EVERY invocation; on macOS the adhoc-signed,
# un-notarized payload then fails Gatekeeper's per-path assessment, and
# because the extraction path is random each run the verdict is never
# cached — measured ~7s startup on Apple Silicon for even `greffer
# --help`. Onedir ships the interpreter + libs unpacked alongside the
# launcher at a STABLE install path, so there is no per-run extraction
# and macOS caches the assessment after first run: startup drops to a
# few hundred ms. Distribution becomes a tarball (greffer/ dir) instead
# of a single file — see cli-release.yml packaging + landing-page
# install.sh/install.ps1 extraction.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir — binaries/datas go in COLLECT below
    name='greffer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX shaves ~5MB but trips macOS Gatekeeper / Apple Silicon
    upx_exclude=[],
    console=True,  # CLI tool — needs stdout/stderr
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # Architecture is whatever the runner builds for
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='greffer',  # output dir: dist/greffer/ (greffer launcher + _internal/)
)
