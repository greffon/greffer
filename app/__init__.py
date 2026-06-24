# Greffer worker version, reported to the manager on register so the
# per-greffon ``min_greffer_version`` compatibility gate can refuse deploying an
# L4 (or any version-floored) greffon onto a greffer too old to run it. Keep in
# sync with pyproject.toml. Single source for the register report.
__version__ = "0.9.0"
