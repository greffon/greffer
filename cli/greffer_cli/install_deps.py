"""``greffer install-deps`` — detect Docker; instruct install if missing.

v1 is detect-and-instruct ONLY on Linux. We deliberately don't run
the official Docker convenience script via sudo from a `curl | sh`
pipeline — that's the highest-risk UX/security bet in the design and
is deferred. macOS / Windows open the Docker Desktop download page.
"""

from __future__ import annotations

import sys
import webbrowser

from . import compose, strings


DOCKER_DESKTOP_URL = "https://docs.docker.com/desktop/"


def detect_and_instruct(greffer_id: str | None) -> int:
    """Return 0 if Docker is already installed and reachable; non-zero
    with an actionable hint printed to stdout otherwise.

    The CLI's `up` composite calls this only when `doctor` flagged
    docker_installed=False — in normal operation the operator goes
    through doctor first.
    """
    version = compose.docker_version()
    if version.ok:
        version_str = _short_version(version.stdout)
        print(strings.INSTALL_DEPS_FOUND.format(version=version_str))
        return 0

    if sys.platform.startswith("linux"):
        # v1: detect + instruct. Print the breadcrumb so the operator
        # knows the exact command to re-run after they install Docker.
        if greffer_id is None:
            greffer_id = "<UUID-from-your-admin>"
        print(strings.INSTALL_DEPS_LINUX_MISSING_DOCKER.format(greffer_id=greffer_id))
        return 1

    # macOS / Windows: open the Docker Desktop download page; print URL
    # as a fallback if no GUI is available.
    print(f"Docker Desktop is required. Opening {DOCKER_DESKTOP_URL} ...")
    try:
        webbrowser.open(DOCKER_DESKTOP_URL)
    except webbrowser.Error:
        pass
    print(f"If the browser didn't open, install from: {DOCKER_DESKTOP_URL}")
    print("When Docker Desktop is running, re-run the install command.")
    return 1


def _short_version(json_stdout: str) -> str:
    import json
    try:
        data = json.loads(json_stdout)
        return data.get("Client", {}).get("Version", "?")
    except (json.JSONDecodeError, AttributeError):
        return "?"
