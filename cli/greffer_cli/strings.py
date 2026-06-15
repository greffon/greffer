"""All operator-facing strings live here.

This module is the single point for ``greffon-marketing`` skill review
at PR time — banned-word check, voice-dial check (plain-spoken 9/10,
anti-hype 10/10), honesty-framework check (no AGPL-as-current-fact
until Phase 0 repos go public; no mention of MCP / custom deploys /
native backup as shipped features; no Stem-the-brand-word in terminal
output, use neutral "the manager's tunnel" instead).

Status glyphs (``✓ ✗ ⊘ ⚠``) follow the same convention as docker,
kubectl, brew — they're status markers, not decorative emojis.
"""

from __future__ import annotations


# --- Success ---------------------------------------------------------

CONNECTED_TUNNEL = """\
✓ Connected — your greffer is ready.
  ID:      {greffer_id}
  Mode:    tunnel (apps served through the manager's tunnel)
  Manager: {manager_url}
Run `greffer status` anytime to check.
"""

CONNECTED_PROXY = """\
✓ Connected — your greffer is ready.
  ID:      {greffer_id}
  Mode:    proxy
  Address: {address}   (manager-callback)
  Manager: {manager_url}
Run `greffer status` anytime to check.
"""

INIT_NO_OP = (
    "~/.greffer/env.env already initialized for this greffer "
    "(ID {greffer_id}) — using existing config."
)

INIT_WROTE_FILES = """\
wrote {compose_path}
wrote {env_path}
  GREFFER_ID={greffer_id}
  GREFFER_MODE={mode}
"""

INIT_WROTE_FILES_PROXY_EXTRA = """\
  GREFFER_ADDRESS={address}      (manager-callback)
"""


# --- State transitions -----------------------------------------------

STATE_STARTING = "[{ts}] Starting — bringing up the greffer container..."
# Printed on ENTERING the Registering state, before the manager has
# confirmed anything. Deliberately neutral: the container is up and
# contacting the manager, but we do NOT yet know the manager received
# the registration — so we must not tell the operator to accept a
# greffer that may never have registered (the silent-stall failure when
# the tunnel is down). The accept guidance (STATE_REGISTERING) prints
# only once the manager actually reports GREFFER_REGISTERING.
STATE_REGISTERING_CONTACTING = (
    "[{ts}] Registering — container is up, contacting the manager..."
)
STATE_REGISTERING = (
    "[{ts}] The manager has received your greffer. Next step: accept it.\n"
    "           greffer ID: {greffer_id}\n"
    "           Open the manager UI, go to Greffers, and click Accept on the\n"
    "           card showing this ID.\n"
    "           (Solo setup? That's you — go accept it now in your manager UI.)"
)
# Heartbeat once the manager has the registration but no one has accepted
# it yet — a genuine "waiting on a human" wait.
STATE_REGISTERING_HEARTBEAT = (
    "[{ts}] still waiting for this greffer to be accepted on the manager "
    "(greffer ID: {greffer_id}). I'll keep checking."
)
# Heartbeat while the manager is STILL at GREFFER_CREATED — it has NOT
# received a registration. This is a connectivity problem on the greffer
# side (tunnel down, egress blocked, workers disabled), not an admin
# action. Keeping it distinct from the "accepted yet?" beat is the whole
# point: the operator can't fix this by clicking Accept.
STATE_REGISTERING_PENDING_HEARTBEAT = (
    "[{ts}] container is up, but the manager hasn't received your "
    "registration yet (greffer ID: {greffer_id}). Still trying."
)
# Printed once while stuck at GREFFER_CREATED, in BOTH modes. The register
# worker POSTs directly to the manager (settings.greffon_base_server) — it
# does NOT go through the tunnel sidecar, which only consumes a client.toml
# the manager pushes AFTER accept. So a stalled registration is a
# greffer→manager reachability problem; the truth is in the register
# worker's own log, not the sidecar's. Point there, and name the real
# causes (manager URL, egress, workers disabled).
STATE_REGISTERING_PENDING_HINT = (
    "[{ts}] the greffer hasn't reached the manager yet. The register worker\n"
    "           POSTs to the manager directly — check its log for the reason:\n"
    "           docker compose -f {compose_path} logs greffer | grep -i regist\n"
    "           Common causes: wrong manager URL, blocked egress to the\n"
    "           manager, or GREFFER_WORKERS_ENABLED not set."
)
STATE_AWAITING_CERT = (
    "[{ts}] Awaiting cert — admin accepted, manager is issuing your TLS cert."
)


# --- Timeout hints ---------------------------------------------------

TIMEOUT_STARTING = """\
✗ Stuck at Starting for {minutes} minutes. Likely causes:
  - Docker daemon down: try `docker info`.
  - Image pull failed: check `docker compose -f {compose_path} logs`.
  - Host port 8001 already in use: `lsof -i :8001`.
  - Docker socket not mounted into the greffer container.
  - Ops-migrations crashed (rare): logs contain an `ApplyOpsMigrations`
    stack trace.
"""

TIMEOUT_REGISTERING = """\
✗ Stuck at Registering for {minutes} minutes. The manager never reached
the accepted state for this greffer. Two different causes:
  - The manager never received the registration (it stayed at
    GREFFER_CREATED). The greffer's register worker couldn't reach the
    manager — it POSTs directly, so check its log for the reason:
      * `docker compose -f {compose_path} logs greffer | grep -i regist`
        (look for "manager not reachable at <url>, retrying").
      * the manager URL might be wrong: re-run `greffer doctor`.
      * the greffer's network egress to the manager may be blocked, or
        GREFFER_WORKERS_ENABLED may be unset (register worker never ran).
  - The manager received it (GREFFER_REGISTERING) but no one accepted —
    open the manager UI, go to Greffers, and accept the card with greffer
    ID {greffer_id}.
  - Check what the greffer is doing:
      docker compose -f {compose_path} logs greffer
"""

TIMEOUT_AWAITING_CERT = """\
✗ Stuck at Awaiting cert for {minutes} minutes. The manager accepted,
but cert install into nginx is failing.
  - Check the nginx sidecar:
      docker compose -f {compose_path} ps
  - Check the greffer's cert-install logs:
      docker compose -f {compose_path} logs greffer | grep cert
"""


# --- Doctor ----------------------------------------------------------

DOCTOR_HEADER = "greffer doctor: pre-flight checks\n"
DOCTOR_PASS_DOCKER = "  ✓ Docker installed ({version})"
DOCTOR_PASS_COMPOSE = "  ✓ Compose plugin available ({version})"
DOCTOR_PASS_DAEMON = "  ✓ Docker daemon reachable"
DOCTOR_PASS_PORT = "  ✓ Host port {port} free"
DOCTOR_PASS_MANAGER = "  ✓ Manager URL {url} is reachable"

DOCTOR_FAIL_DOCKER = (
    "  ✗ Docker NOT installed\n"
    "    → install Docker first: https://docs.docker.com/engine/install/"
)
DOCTOR_FAIL_DAEMON = (
    "  ✗ Docker daemon NOT reachable\n"
    "    → start Docker first: `systemctl start docker` (Linux) or open Docker Desktop (Mac/Windows)."
)
DOCTOR_FAIL_COMPOSE = (
    "  ✗ Compose plugin NOT available\n"
    "    → install docker-compose-plugin: https://docs.docker.com/compose/install/"
)
DOCTOR_FAIL_PORT = (
    "  ✗ Host port {port} already in use\n"
    "    → another greffer? or another service. Run `lsof -i :{port}` to find out."
)
DOCTOR_FAIL_MANAGER = (
    "  ✗ Manager URL {url} is unreachable\n"
    "    → check the URL; common transport errors: DNS, TCP refused, TLS handshake."
)
DOCTOR_SKIP = "  ⊘ {what} (skipped — {reason})"

DOCTOR_ALL_PASSED = "\nAll checks passed."
DOCTOR_FAILED_SUMMARY = "\n{n_failed} check(s) failed. Fix the issues above and re-run."


# --- install-deps ----------------------------------------------------

INSTALL_DEPS_LINUX_MISSING_DOCKER = """\
Docker is not installed on this host. The greffer CLI needs Docker
to run the greffer container.

Install Docker via the official guide for your distro:
  https://docs.docker.com/engine/install/

When Docker is running, re-run the exact install command your admin
gave you. The minimum form is:

  curl -sSL https://greffon.io/install.sh | sh -s -- up \\
    --id {greffer_id}

If your admin's command had additional flags (--manager for a
self-hosted manager, or --mode proxy --address ... --public-host ...
for direct-exposure mode), re-run that exact one.
"""

INSTALL_DEPS_FOUND = "Docker found ({version}). Continuing."


# --- Errors ----------------------------------------------------------

ERR_PLACEHOLDER_NOT_SUBSTITUTED = """\
One or more values in the install command look like literal placeholders
that weren't substituted:
  --address    <YOUR-ADDRESS>        (the manager-callback hostname)
  --public-host <YOUR-PUBLIC-HOST>   (your public IP / hostname for end-users)

Replace the placeholders in the command with the real values and re-run.

What are these two values?
  --address    is how the manager reaches THIS greffer (control plane).
               Usually a hostname like `mygreffer.example.com`.
  --public-host is what END USERS see when they browse to greffon apps
               running on this greffer (proxy mode). Usually your
               public IP (e.g. 203.0.113.10) or a public DNS name.
               If you only have one value for both, use the same value twice.
"""

ERR_INIT_DIFFERENT_ID = """\
This host is already initialized for greffer {existing_id} but the
install command you ran is for greffer {new_id}.

To intentionally re-init for a different greffer:
  1. Stop the current greffer:
       docker compose -f {compose_path} down
  2. Ask your admin to delete the old greffer in the manager admin UI:
       {manager_url}/admin/greffer/greffer/{existing_id}/
  3. Remove the local config:
       rm -rf {config_dir}
  4. Re-run the install command.

v1 deliberately has no --force flag — re-init is rare enough that we
prefer the deliberate three-step path over a silent overwrite that
strands a manager-side row.
"""

ERR_GREFFER_ID_CLAIMED = """\
This greffer ID is already claimed by another host at {claimed_address}.
The manager returned 409 Conflict on register.

If you intended to move the greffer to this host, ask your admin to
delete the old row first via the manager admin UI:
  {manager_url}/admin/greffer/greffer/{greffer_id}/

Then re-run this install command.
"""


# --- Reachability self-test (proxy mode only) ------------------------

REACHABILITY_OK = "✓ network reachable at your public host."

REACHABILITY_WRONG_ID = """\
⚠ Connected to the manager, but {public_host}:{port}
is responding as a different greffer (id {seen}, expected {expected}).
Usually a typo in --public-host that resolves to another live host.
Re-check; if intentional (e.g. a load balancer fronting multiple
greffers), proceed manually."""

REACHABILITY_TRANSPORT_ERROR = """\
⚠ Connected to the manager, but {public_host}:{port}
is unreachable from this host. Common causes:
  (a) NAT hairpinning — your network can't reach its own public IP
      from inside. Normal on home routers; harmless if external
      clients can reach you.
  (b) --public-host doesn't point at this host (DNS/IP wrong).
  (c) Firewall blocks.
Test from outside:
  curl -k https://{public_host}:{port}/healthz"""

REACHABILITY_BAD_STATUS = (
    "⚠ reachable but greffer is not responding cleanly; "
    "try `docker compose logs greffer`."
)


# --- greffer update --------------------------------------------------

UPDATE_NO_TARGET = (
    "✗ Can't determine the latest greffer version (the version manifest is "
    "unreachable).\n  → re-run with an explicit target: `greffer update --to <version>`"
)

UPDATE_CHECK = """\
greffer update --check
  current:   {current}
  target:    {target}
  available: {available}
(no changes made)
"""

UPDATE_PREFLIGHT_NO_DATA_VOLUME = (
    "✗ Refusing to update: /data is not a persistent named volume.\n"
    "  An update recreates the container; without a named /data volume the\n"
    "  greffer would lose its identity and fail to re-register. Fix the\n"
    "  docker-compose.yml /data mount (or re-run `greffer up`) and try again."
)

UPDATE_NEEDS_CONFIRM_NO_ROLLBACK = (
    "✗ Rollback safety for {current} → {target} can't be confirmed "
    "(the release is flagged no-in-place-rollback, or the version manifest is "
    "unreachable).\n  → re-run with `--confirm-no-rollback` to proceed anyway."
)

UPDATE_ALREADY = "✓ Already up to date (running {target}). Nothing to do."

UPDATE_PULL_FAILED = (
    "✗ Couldn't pull the {target} images. The compose file was restored and "
    "the greffer is still running its previous version. Check connectivity to "
    "the image registry and retry."
)

UPDATE_OK = "✓ Updated to {target}. The greffer re-registered and is ready."

UPDATE_GATE_FAILED = (
    "✗ The {reason} check failed after recreate — rolling back to the previous "
    "version."
)

UPDATE_ROLLED_BACK = (
    "✓ Rolled back to the previous version, which is healthy. The update did "
    "not apply; see the logs above for why."
)

UPDATE_ROLLBACK_FAILED = (
    "✗ The update failed AND the rollback did not come back healthy. Manual "
    "recovery needed: inspect `docker compose -f {compose_path} ps` and logs, "
    "then re-run `greffer up` once the cause is fixed."
)
