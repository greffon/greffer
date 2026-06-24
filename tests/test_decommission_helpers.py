from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from apps.utils.docker import compose, volume

_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_list_instance_volumes_prefix_filters_substring_matches():
    """Docker's name= filter is an unanchored substring match; we must keep only
    volumes that actually START with `<id>_`, not ones that merely contain it."""
    out = (f"{_ID}_data\n"
           f"{_ID}_db\n"
           f"other-{_ID}_x\n"   # contains the id mid-name -> must be excluded
           "\n")
    with patch("apps.utils.docker.volume.subprocess.run",
               return_value=SimpleNamespace(stdout=out, returncode=0, stderr="")):
        names = volume.list_instance_volumes(_ID)
    assert names == [f"{_ID}_data", f"{_ID}_db"]


def test_remove_instance_volumes_force_rms_each_and_returns_names():
    calls = []

    def _run(args, **kw):
        calls.append(args)
        # the ls call returns the two volumes; the rm calls succeed
        if args[:3] == ["docker", "volume", "ls"]:
            return SimpleNamespace(stdout=f"{_ID}_data\n{_ID}_db\n",
                                   returncode=0, stderr="")
        return SimpleNamespace(stdout="", returncode=0, stderr="")

    with patch("apps.utils.docker.volume.subprocess.run", side_effect=_run):
        removed = volume.remove_instance_volumes(_ID)

    assert removed == [f"{_ID}_data", f"{_ID}_db"]
    rm_calls = [c for c in calls if c[:3] == ["docker", "volume", "rm"]]
    assert rm_calls == [["docker", "volume", "rm", "-f", f"{_ID}_data"],
                        ["docker", "volume", "rm", "-f", f"{_ID}_db"]]


def test_remove_instance_volumes_returns_names_even_if_rm_fails():
    """An in-use volume can't be force-removed; remove_instance_volumes is
    best-effort and still returns the attempted names (the caller's verify
    re-lists to detect the survivor)."""
    def _run(args, **kw):
        if args[:3] == ["docker", "volume", "ls"]:
            return SimpleNamespace(stdout=f"{_ID}_data\n", returncode=0, stderr="")
        return SimpleNamespace(stdout="", returncode=1, stderr="volume is in use")

    with patch("apps.utils.docker.volume.subprocess.run", side_effect=_run):
        removed = volume.remove_instance_volumes(_ID)
    assert removed == [f"{_ID}_data"]


def test_down_is_noop_without_compose_file(monkeypatch, tmp_path):
    """No compose file (instance never started / already torn down) -> down is a
    no-op (None), and docker-compose is never invoked."""
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    with patch("apps.utils.docker.compose.subprocess.run") as run:
        assert compose.down(_ID) is None
        run.assert_not_called()


def test_down_runs_waited_down_v_when_compose_present(monkeypatch, tmp_path):
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    inst_dir = tmp_path / _ID
    inst_dir.mkdir()
    (inst_dir / "docker-compose.yml").write_text("services: {}\n")
    with patch("apps.utils.docker.compose.subprocess.run") as run:
        compose.down(_ID)
    args = run.call_args[0][0]
    assert args[:4] == ["docker-compose", "-p", _ID, "-f"]
    assert args[-3:] == ["down", "-v", "--remove-orphans"]
    # WAITED: subprocess.run, with a bounded timeout
    assert run.call_args.kwargs.get("timeout") == compose._DOWN_TIMEOUT_SECONDS
