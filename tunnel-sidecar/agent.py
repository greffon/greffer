#!/usr/bin/env python3
"""Pull agent for the greffer tunnel sidecar.

Polls the manager's ``GET /api/greffer/{id}/tunnel-config/`` endpoint,
writes the returned rathole ``client.toml`` atomically, and supervises a
rathole-client subprocess. rathole's built-in file watcher picks up
config changes — the agent never sends SIGHUP and never needs the Docker
socket.

Environment:
    GREFFER_ID (required)          — UUID of this greffer in manager.
    GREFFER_TOKEN or
      GREFFER_TOKEN_FILE (required) — X-GREFFON-TOKEN credential. File path
      preferred for container secret mounts.
    MANAGER_URL (required)         — e.g. https://api.greffon.local:8443
    POLL_INTERVAL_SECONDS (opt)    — default 2.
    CA_BUNDLE_PATH (opt)           — default /secrets/ca.pem.
"""
import hashlib
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import requests

CONFIG_PATH = Path('/etc/rathole/client.toml')
DEFAULT_CA_BUNDLE_PATH = '/secrets/ca.pem'
DEFAULT_POLL_INTERVAL = 2.0
BACKOFF_MAX = 30.0
# Short per-request timeout bounds the worst-case shutdown delay: on
# SIGTERM the signal handler also calls session.close(), so any in-flight
# request raises promptly. Kept at 3s so a slow but healthy manager
# (mid-reload) is still tolerated — the 5s container-stop grace period
# common in container runtimes stays clear of SIGKILL.
REQUEST_TIMEOUT = 3.0
USER_AGENT = 'greffer-tunnel-sidecar/1.0'

logger = logging.getLogger('tunnel-sidecar')

# Event-based shutdown so signal handlers can interrupt a blocking wait
# immediately. A bool + time.sleep(backoff) would leave the loop sleeping
# up to BACKOFF_MAX seconds after SIGTERM, causing orchestrators with short
# stop timeouts to fall back to SIGKILL before the agent exits cleanly.
_shutdown_event = threading.Event()


def _install_signal_handlers(state):
    def _handler(signum, _frame):
        logger.info('signal_received signal=%s', signum)
        _shutdown_event.set()
        rathole = state.get('rathole')
        if rathole and rathole.poll() is None:
            rathole.terminate()
        session = state.get('session')
        if session is not None:
            # Best-effort: closing the session drops the underlying sockets,
            # so an in-flight session.get() raises promptly via
            # requests.RequestException rather than blocking for REQUEST_TIMEOUT
            # while shutdown is already in progress.
            try:
                session.close()
            except Exception:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handler)


def _load_token() -> str:
    token_file = os.environ.get('GREFFER_TOKEN_FILE')
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as exc:
            # Translate unreadable / missing file into RuntimeError so
            # main()'s startup-config-error path runs and exits cleanly,
            # rather than letting the OSError escape as an unhandled
            # traceback.
            raise RuntimeError(
                f'cannot read GREFFER_TOKEN_FILE={token_file}: {exc}'
            ) from exc
        if not token:
            # Empty / whitespace-only secret file would produce endless
            # 401 polling against manager — fail loud with the file path
            # so an operator can fix the mount or the secret.
            raise RuntimeError(
                f'GREFFER_TOKEN_FILE={token_file} is empty or whitespace-only'
            )
        return token
    token = os.environ.get('GREFFER_TOKEN')
    if not token:
        raise RuntimeError(
            'one of GREFFER_TOKEN or GREFFER_TOKEN_FILE must be set'
        )
    return token


def _resolve_poll_interval() -> float:
    """Validate POLL_INTERVAL_SECONDS at startup. Without this, ``0`` /
    negative values produce a hot loop (wait(timeout=0) returns
    immediately) and a non-numeric value would crash module import
    before main() can emit a structured startup error log."""
    raw = os.environ.get('POLL_INTERVAL_SECONDS')
    if raw is None or raw == '':
        return DEFAULT_POLL_INTERVAL
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f'POLL_INTERVAL_SECONDS must be numeric, got {raw!r}'
        ) from exc
    if value <= 0:
        raise RuntimeError(
            f'POLL_INTERVAL_SECONDS must be > 0, got {value}'
        )
    return value


def _resolve_ca_bundle():
    """Return the verify= value for ``requests``.

    If CA_BUNDLE_PATH is explicitly set, use it verbatim — requests will
    raise at call-time if the file is missing, which is the right signal
    for a misconfigured operator intent.

    If CA_BUNDLE_PATH is not set and the default `/secrets/ca.pem` is
    absent, fall back to ``True`` (system CA store). This keeps the
    sidecar functional in deployments that rely on Let's Encrypt /
    public CA for the manager, without forcing a dummy secret mount.
    """
    explicit = os.environ.get('CA_BUNDLE_PATH')
    if explicit:
        return explicit
    if Path(DEFAULT_CA_BUNDLE_PATH).exists():
        return DEFAULT_CA_BUNDLE_PATH
    logger.info(
        'ca_bundle_using_system_store default_path=%s absent',
        DEFAULT_CA_BUNDLE_PATH,
    )
    return True


def _atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(body)
    os.replace(tmp, path)


def _start_rathole() -> subprocess.Popen:
    proc = subprocess.Popen(['rathole', str(CONFIG_PATH)])
    logger.info('rathole_started pid=%s', proc.pid)
    return proc


def poll_once(session, url, token, etag, ca_bundle):
    """Single GET against /tunnel-config/. Returns (etag, body_or_None).

    Raises ``requests.HTTPError`` on 5xx so the main loop's RequestException
    branch applies exponential backoff — without this, many sidecars
    re-poll a struggling manager every POLL_INTERVAL (2s by default) and
    amplify load during an outage. 4xx responses are logged at base
    interval since they typically reflect operator-fixable state
    (bad token, greffer not provisioned yet) rather than overload.
    """
    headers = {'X-GREFFON-TOKEN': token, 'User-Agent': USER_AGENT}
    if etag:
        headers['If-None-Match'] = etag
    resp = session.get(
        url, headers=headers, verify=ca_bundle, timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 304:
        return etag, None
    if resp.status_code == 200:
        return resp.headers.get('ETag'), resp.text
    if 500 <= resp.status_code < 600:
        # Triggers the main loop's exponential backoff via the existing
        # except requests.RequestException branch (HTTPError is a subclass).
        resp.raise_for_status()
    # 401/403 (bad token), 404 (greffer gone / not ready yet / mode-mismatch
    # masked as 404 to avoid oracles). Operator-fixable; keep polling.
    logger.warning('unexpected_status status=%s', resp.status_code)
    return etag, None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        greffer_id = os.environ['GREFFER_ID']
        manager_url = os.environ['MANAGER_URL'].rstrip('/')
        # Validate one of the token sources is configured at startup —
        # but DO NOT cache the loaded value. Greffer can mint a fresh
        # token on its own restart (it's the default behavior) and
        # rewrite the shared file; the agent re-loads on every poll
        # below so a fresh token propagates within one POLL_INTERVAL.
        if not os.environ.get('GREFFER_TOKEN_FILE') and not os.environ.get('GREFFER_TOKEN'):
            raise RuntimeError(
                'one of GREFFER_TOKEN or GREFFER_TOKEN_FILE must be set'
            )
        poll_interval = _resolve_poll_interval()
    except (KeyError, RuntimeError) as exc:
        logger.error('startup_config_missing error=%s', exc)
        return 2
    ca_bundle = _resolve_ca_bundle()
    url = f'{manager_url}/api/greffer/{greffer_id}/tunnel-config/'
    session = requests.Session()
    state = {'rathole': None, 'session': session}
    _install_signal_handlers(state)
    logger.info(
        'agent_started greffer_id=%s manager=%s poll_interval_s=%s',
        greffer_id, manager_url, poll_interval,
    )

    etag = None
    current_hash = None
    backoff = poll_interval
    while not _shutdown_event.is_set():
        try:
            token = _load_token()
        except RuntimeError as exc:
            # Token file missing / empty — greffer hasn't published yet,
            # the shared volume isn't mounted, or the file got truncated
            # mid-rotation. Back off and retry. Without this, a brief
            # transient (greffer restart) would crash the agent.
            logger.warning('token_load_failed error=%s', exc)
            backoff = min(backoff * 2, BACKOFF_MAX)
            if _shutdown_event.wait(timeout=backoff):
                break
            continue
        try:
            new_etag, new_body = poll_once(
                session, url, token, etag, ca_bundle,
            )
            if new_body is not None:
                new_hash = hashlib.sha256(new_body.encode()).hexdigest()
                if new_hash != current_hash:
                    _atomic_write(CONFIG_PATH, new_body)
                    if state['rathole'] is None:
                        state['rathole'] = _start_rathole()
                    # Subsequent changes: rathole-client's built-in file
                    # watcher picks up client.toml automatically.
                    current_hash = new_hash
                    logger.info('config_applied etag=%s', new_etag)
            etag = new_etag
            backoff = poll_interval
        except requests.RequestException as exc:
            logger.warning('poll_error error=%s', exc)
            backoff = min(backoff * 2, BACKOFF_MAX)

        rathole = state['rathole']
        if rathole and rathole.poll() is not None and not _shutdown_event.is_set():
            logger.error('rathole_exited code=%s', rathole.returncode)
            return 1
        # Event-wait is signal-aware: a SIGTERM/SIGINT during the wait
        # sets the event and wakes us immediately, bounding shutdown
        # latency to the time it takes rathole to SIGTERM-exit (handled
        # below), not the full backoff interval.
        if _shutdown_event.wait(timeout=backoff):
            break

    rathole = state['rathole']
    if rathole and rathole.poll() is None:
        try:
            rathole.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning('rathole_kill pid=%s (did not exit in 5s)', rathole.pid)
            rathole.kill()
    logger.info('agent_shutdown')
    return 0


if __name__ == '__main__':
    sys.exit(main())
