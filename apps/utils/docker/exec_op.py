"""Low-level ``docker exec`` for Phase 3 app-aware hooks (pg_dump / quiesce /
dump-validation) and migration.

Uses the docker-py LOW-LEVEL API (``exec_create`` -> ``exec_start`` ->
``exec_inspect``) ON PURPOSE: the high-level ``container.exec_run(stream=True)``
returns ``exit_code=None`` and hides the exec id, so a failed hook (e.g. a
TRUNCATED pg_dump) would record success (epic HLD A3). Here the REAL container
``ExitCode`` is read from ``exec_inspect`` after the command finishes, so the
caller can gate on it.

Security: argv is always a LIST, never a shell string -- the catalog is
semi-trusted (it already has an SSTI sandbox), so hooks get NO shell-injection
surface. Secrets travel via ``environment`` (name->value), never the argv.
"""
import logging
from dataclasses import dataclass

import docker

from apps.utils.docker.compose import client

logger = logging.getLogger("greffer")

_DOCKER_ERRORS = (docker.errors.DockerException, OSError)


@dataclass
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ExecError(Exception):
    """The exec could not be created/started/inspected (docker-level failure),
    as distinct from the command running and exiting non-zero (-> ExecResult
    with a non-zero exit_code)."""


def exec_in_container(container, argv, *, environment=None) -> ExecResult:
    """Run ``argv`` (a list) inside a RUNNING container and return its real
    exit code + captured stdout/stderr (buffered -- for BOUNDED hooks like a
    pg_dump validation or a quiesce, NOT a whole-DB stream).

    ``argv`` MUST be a list (shell-free). ``environment`` is a name->value dict
    passed to the exec env (secrets here, never in argv). Raises ``ExecError``
    on a docker-level failure; a command that runs and fails is a normal
    ``ExecResult`` with ``exit_code != 0`` (the caller gates on ``.ok``)."""
    if isinstance(argv, str):
        raise ExecError("argv must be a list (shell-free), not a string")
    if not argv:
        raise ExecError("argv must be a non-empty list")
    api = client.api
    try:
        exec_id = api.exec_create(
            container.id, argv,
            environment=environment, stdout=True, stderr=True,
        )["Id"]
        # stream=False blocks until the command completes and buffers output;
        # demux=True splits stdout/stderr so a dump on stdout isn't polluted by
        # a notice on stderr.
        # CONTRACT (docker SDK 5.0.3): exec_start(stream=False, demux=True) runs
        # consume_socket_output to socket EOF (blocks until the command exits)
        # and returns a (stdout|None, stderr|None) 2-tuple -- a slot is None when
        # that stream produced no frames, never a bare bytes or a generator (that
        # is the stream=True path). The pending docker ^7.0 bump keeps this demux
        # contract; re-verify it here if that return shape ever changes.
        output = api.exec_start(exec_id, demux=True)
        info = api.exec_inspect(exec_id)
    except _DOCKER_ERRORS as exc:
        raise ExecError(str(exc)) from exc

    exit_code = info.get("ExitCode")
    if exit_code is None:
        # The daemon should always report ExitCode once the command finished;
        # a None here means we cannot trust success -- treat as failure.
        logger.warning("exec_inspect_no_exit_code container=%s argv0=%s",
                       container.id[:12], argv[0] if argv else "")
        exit_code = -1
    stdout, stderr = output if output is not None else (None, None)
    return ExecResult(exit_code=exit_code, stdout=stdout or b"", stderr=stderr or b"")
