#!/bin/bash
set -euo pipefail
mkdir -p /etc/rathole
# Agent is the orchestrator: it polls manager for the first valid config
# and only then spawns rathole-client. Avoids the "does rathole accept an
# empty services config at boot?" question — rathole never boots without
# a real config.
exec python3 /sidecar/agent.py
