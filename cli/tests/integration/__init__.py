"""Integration smoke tests for the greffer CLI.

These tests exercise the full Typer CLI entrypoint against a real
in-process HTTP server (mock manager) and monkeypatched docker
wrappers (mock docker). They sit one layer above the unit tests
in ``../`` which mock at the function-call level — these catch
HTTP-layer issues (URL construction, header handling, JSON
parsing), arg-parsing issues, and exit-code contracts that unit
tests bypass.

Cost: ~1s per scenario. Runs in-process — no daemon, no real
manager, no cert minting.

What's deliberately NOT covered here:
- Real Docker daemon (would require Docker-in-Docker or a real
  daemon on the CI runner; not worth the matrix complexity)
- Real manager backend (would require docker-compose orchestration
  per test — a different category of test entirely)
- install.sh / install.ps1 script logic (those are shell-script
  level; would need real OS provisioning to test end-to-end)

This is the "hermetic black-box CLI test" lane.
"""
