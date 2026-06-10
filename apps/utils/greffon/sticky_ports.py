"""Sticky L4 host-port persistence.

An L4 (Tier-C) instance's public endpoint is ``host:port`` — baked into VPN
client configs and game bookmarks, and persisted inside the app itself
(wg-easy stores the advertised endpoint in its DB). So the allocated host port
must survive restarts, or every distributed client config breaks. We persist
the per-port allocation in a small JSON sidecar in the instance directory under
``$GREFFON_PATH`` (next to the rendered docker-compose.yml / nginx.conf), and
reuse it on the next start when the port is still free.

Reservation policy (see followup-port-semantics.md):
- ``same_port`` ports: the app baked the port in, so a rotation breaks the
  datapath — reuse is load-bearing. We keep the sidecar entry for the
  instance's lifetime and only fall back to a fresh port if the sticky one is
  genuinely no longer bindable.
- plain L4 ports: best-effort reuse; a fresh port on conflict is fine (the
  card re-renders the new endpoint).

The sidecar is NOT cleaned up on delete (the greffer has no delete endpoint;
the whole instance dir already leaks) — it's correctly reused if the same
instance id is restored.
"""
from __future__ import annotations

import fcntl
import json
import os

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
    """Persist {port_name: host_port} for the instance (exclusive-locked write).

    The instance dir already exists at start time (compose/nginx are written
    there); create it defensively anyway so a caller can't crash on a missing
    dir. Concurrent starts in the single greffer process run in a threadpool,
    so the flock serializes the read-modify-write.
    """
    inst_dir = os.path.join(str(greffon_path), instance_id)
    os.makedirs(inst_dir, exist_ok=True)
    path = os.path.join(inst_dir, _SIDECAR_NAME)
    payload = {str(k): int(v) for k, v in mapping.items()}
    with open(path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
