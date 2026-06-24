from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import docker
import pytest

from apps.utils.docker import exec_op


def _container(cid="containerid123456"):
    return SimpleNamespace(id=cid)


def _fake_api(*, exit_code=0, stdout=b"out", stderr=b"err"):
    api = MagicMock()
    api.exec_create.return_value = {"Id": "exec-1"}
    api.exec_start.return_value = (stdout, stderr)
    api.exec_inspect.return_value = {"ExitCode": exit_code}
    return api


def test_exec_success_returns_real_exit_code_and_streams():
    api = _fake_api(exit_code=0, stdout=b"DUMP", stderr=b"notice")
    with patch.object(exec_op.client, "api", api):
        res = exec_op.exec_in_container(_container(), ["pg_dump", "-Fc"])
    assert res.ok and res.exit_code == 0
    assert res.stdout == b"DUMP" and res.stderr == b"notice"
    # low-level path used (the A3-correct one), with argv as a list
    api.exec_create.assert_called_once()
    assert api.exec_create.call_args[0][1] == ["pg_dump", "-Fc"]


def test_exec_nonzero_exit_is_not_ok():
    api = _fake_api(exit_code=1, stdout=b"", stderr=b"could not connect")
    with patch.object(exec_op.client, "api", api):
        res = exec_op.exec_in_container(_container(), ["pg_dump"])
    assert not res.ok and res.exit_code == 1
    assert res.stderr == b"could not connect"


def test_exec_inspect_missing_exit_code_is_failure():
    # A None ExitCode (the exec_run(stream=True) trap) must NOT read as success.
    api = _fake_api()
    api.exec_inspect.return_value = {}  # no ExitCode
    with patch.object(exec_op.client, "api", api):
        res = exec_op.exec_in_container(_container(), ["whoami"])
    assert not res.ok and res.exit_code == -1


def test_exec_passes_environment_not_argv():
    api = _fake_api()
    with patch.object(exec_op.client, "api", api):
        exec_op.exec_in_container(_container(), ["sh", "-c", "echo"],
                                  environment={"PGPASSWORD": "secret"})
    assert api.exec_create.call_args.kwargs["environment"] == {"PGPASSWORD": "secret"}


def test_exec_rejects_string_argv():
    with pytest.raises(exec_op.ExecError):
        exec_op.exec_in_container(_container(), "pg_dump -Fc")  # shell string


def test_exec_wraps_docker_error():
    api = MagicMock()
    api.exec_create.side_effect = docker.errors.APIError("daemon down")
    with patch.object(exec_op.client, "api", api):
        with pytest.raises(exec_op.ExecError):
            exec_op.exec_in_container(_container(), ["whoami"])


def test_exec_handles_none_output():
    api = _fake_api()
    api.exec_start.return_value = None  # no output captured
    with patch.object(exec_op.client, "api", api):
        res = exec_op.exec_in_container(_container(), ["true"])
    assert res.stdout == b"" and res.stderr == b"" and res.ok
