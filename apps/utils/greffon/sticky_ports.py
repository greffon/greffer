"""Sticky L4 host-port persistence.

An L4 (Tier-C) instance's public endpoint is ``host:port`` — baked into VPN
client configs and game bookmarks, and persisted inside the app itself
(wg-easy stores the advertised endpoint in its DB). So the allocated host port
must survive restarts, or every distributed client config breaks. We persist
the per-port allocation in a small JSON sidecar in the instance directory under
``$GREFFON_PATH`` (next to the rendered docker-compose.yml / nginx.conf), and
reuse it on the next start when the port is still free.

All L4 ports are sticky (``same_port`` and plain alike): reuse the persisted
port when it's still free, else take the lowest free one. "Free" is decided
against the ports the docker daemon publishes for RUNNING containers (see
apps/utils/docker/l4_ports.py), NOT a socket bind-probe: the greffer runs in
its own container network namespace and is blind to host bindings, so a probe
reads a host-occupied port as free and hands the same number to two instances.
A stopped instance's port is genuinely free for others (docker releases a host
port on stop), so persisting does not deplete the dedicated L4 range; it just
lets an instance reclaim its endpoint on restart when nothing else took it. For
``same_port`` ports a stable endpoint is load-bearing (the app baked the port
into its own config); on a proxy greffer a taken same_port endpoint is a hard
error rather than a silent rotation, while a plain-L4 (or tunnel) rotation is
harmless.

The sidecar is NOT cleaned up on delete (the greffer has no delete endpoint;
the whole instance dir already leaks) — it's correctly reused if the same
instance id is restored.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile

_SIDECAR_NAME = "l4_ports.json"


def _sidecar_path(greffon_path, instance_id):
    return os.path.join(str(greffon_path), instance_id, _SIDECAR_NAME)


def load(greffon_path, instance_id):
    """Return the persisted {port_name: host_port} map (empty if none/corrupt)."""
    path = _sidecar_path(greffon_path, instance_id)
    try:
        with open(path) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to {str: int}; drop anything malformed rather than crash a start.
    out = {}
    for name, port in data.items():
        try:
            out[str(name)] = int(port)
        except (ValueError, TypeError):
            continue
    return out


def save(greffon_path, instance_id, mapping):
    """Persist {port_name: host_port} for the instance, atomically.

    Write to a temp file in the same dir, fsync, then ``os.replace`` onto the
    target — so a concurrent reader (or a crash mid-write) never sees a
    truncated/partial sidecar (which ``load`` would treat as empty and rotate a
    sticky port off). The instance dir already exists at start time
    (compose/nginx are written there); create it defensively anyway.

    NOTE: this makes the *write* atomic, but it does NOT serialize a full
    load->decide->save across two concurrent starts of the SAME instance (each
    is its own lock cycle). Concurrent same-instance starts are a broader
    greffer concern (they also race on docker-compose up) and are out of scope
    here; the practical guard is that the manager does not issue overlapping
    starts for one instance.
    """
    inst_dir = os.path.join(str(greffon_path), instance_id)
    os.makedirs(inst_dir, exist_ok=True)
    path = os.path.join(inst_dir, _SIDECAR_NAME)
    payload = {str(k): int(v) for k, v in mapping.items()}
    # Unique tmp name (mkstemp, matching tunnel_config / ops_migrations) so two
    # concurrent saves don't share one ".tmp" and have the second's os.replace
    # hit a vanished file; also cleaned up if json.dump raises mid-write.
    fd, tmp = tempfile.mkstemp(dir=inst_dir, prefix=".l4-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
