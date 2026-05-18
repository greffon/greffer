"""Typer entry point — wires the four operator subcommands."""

from __future__ import annotations

import re
import sys
from importlib import resources
from pathlib import Path
from typing import Optional

import typer

from . import compose as compose_mod
from . import doctor as doctor_mod
from . import install_deps as install_deps_mod
from . import paths
from . import status as status_mod
from . import strings as strings_mod
from . import up as up_mod


app = typer.Typer(
    name="greffer",
    help="Operator CLI for installing and managing a greffer.",
    no_args_is_help=True,
)


DEFAULT_MANAGER = "https://api.greffon.io"
_ADDRESS_PLACEHOLDER = "<YOUR-ADDRESS>"
_PUBLIC_HOST_PLACEHOLDER = "<YOUR-PUBLIC-HOST>"
# RFC 1123 hostname OR IPv4 (we accept IPv6 brackets too; the regex
# is lenient — operator typos will fail loudly when curl tries to reach.
_HOSTNAME_OR_IP_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$|"
    r"^\d{1,3}(\.\d{1,3}){3}$|"
    r"^\[[0-9a-fA-F:]+\]$"
)


@app.command()
def doctor(
    manager: Optional[str] = typer.Option(
        None, "--manager", help="Manager URL to probe (defaults to env.env if present)",
    ),
    config_dir: Optional[str] = typer.Option(
        None, "--config-dir", help="Override the default ~/.greffer/ location",
    ),
) -> None:
    """Run read-only preflight checks."""
    cfg = paths.resolve_config_dir(config_dir)
    # Pick up the manager URL from env.env if the operator didn't pass --manager.
    if not manager:
        from . import env_file as env_mod
        env = env_mod.EnvFile.read(paths.env_env_path(cfg))
        manager = env.get("GREFFON_BASE_SERVER")

    results = doctor_mod.run(manager)
    print(doctor_mod.format_report(results))
    if doctor_mod.is_blocking_failure(results):
        raise typer.Exit(code=1)


@app.command("install-deps")
def install_deps_cmd(
    id: Optional[str] = typer.Option(
        None, "--id", help="Greffer ID (used in the re-run breadcrumb)",
    ),
) -> None:
    """Detect Docker; instruct install if missing."""
    rc = install_deps_mod.detect_and_instruct(id)
    raise typer.Exit(code=rc)


@app.command()
def up(
    id: str = typer.Option(..., "--id", help="Manager-issued greffer UUID"),
    manager: str = typer.Option(
        DEFAULT_MANAGER, "--manager", help="Manager URL (defaults to Greffon Hosted)",
    ),
    mode: str = typer.Option(
        "tunnel", "--mode", help="Deployment mode: tunnel (default) or proxy",
    ),
    address: Optional[str] = typer.Option(
        None, "--address", help="Manager-callback hostname (required in proxy mode)",
    ),
    public_host: Optional[str] = typer.Option(
        None, "--public-host",
        help="End-user-facing public IP/hostname (required in proxy mode)",
    ),
    config_dir: Optional[str] = typer.Option(
        None, "--config-dir", help="Override the default ~/.greffer/ location",
    ),
    timeout: int = typer.Option(
        600, "--timeout", help="Per-state timeout in seconds (default 10 min)",
    ),
) -> None:
    """All-in-one: write config + start container + register with manager."""
    if mode not in ("tunnel", "proxy"):
        typer.echo(f"--mode must be 'tunnel' or 'proxy' (got: {mode})", err=True)
        raise typer.Exit(code=2)

    # Placeholder detection: refuse to run with literal <YOUR-*> values.
    if address == _ADDRESS_PLACEHOLDER or public_host == _PUBLIC_HOST_PLACEHOLDER:
        typer.echo(strings_mod.ERR_PLACEHOLDER_NOT_SUBSTITUTED, err=True)
        raise typer.Exit(code=2)

    if mode == "proxy":
        if not address or not public_host:
            typer.echo(
                "--mode proxy requires --address and --public-host.\n"
                "(In tunnel mode — the default — they're not needed.)",
                err=True,
            )
            raise typer.Exit(code=2)
        # RFC 1123 / IP validation.
        if not _HOSTNAME_OR_IP_RE.match(address):
            typer.echo(f"--address is not a valid hostname or IP: {address}", err=True)
            raise typer.Exit(code=2)
        if not _HOSTNAME_OR_IP_RE.match(public_host):
            typer.echo(
                f"--public-host is not a valid hostname or IP: {public_host}", err=True,
            )
            raise typer.Exit(code=2)

    cfg = paths.resolve_config_dir(config_dir)
    cfg.mkdir(parents=True, exist_ok=True)

    # Idempotence: if env.env exists with matching id, fast-path.
    existing_id = up_mod.existing_greffer_id(cfg)
    if existing_id == id:
        typer.echo(strings_mod.INIT_NO_OP.format(greffer_id=id))
        # Skip config write; container should already be running. Fall
        # through to the state-machine which fast-paths if already
        # Connected.
    elif existing_id is not None:
        typer.echo(strings_mod.ERR_INIT_DIFFERENT_ID.format(
            existing_id=existing_id,
            new_id=id,
            compose_path=paths.docker_compose_yml_path(cfg),
            manager_url=manager,
            config_dir=cfg,
        ), err=True)
        raise typer.Exit(code=1)
    else:
        # First run on this host: write config.
        env_values = up_mod._build_env_values(
            manager_url=manager,
            greffer_id=id,
            mode=mode,  # type: ignore[arg-type]
            address=address,
            public_host=public_host,
        )
        template_path = _compose_template_path()
        template_text = template_path.read_text(encoding="utf-8")
        image_tag = _read_image_tag()
        up_mod.write_config(
            cfg, template_text, image_tag, env_values=env_values,
        )
        typer.echo(strings_mod.INIT_WROTE_FILES.format(
            compose_path=paths.docker_compose_yml_path(cfg),
            env_path=paths.env_env_path(cfg),
            greffer_id=id,
            mode=mode,
        ))
        if mode == "proxy":
            typer.echo(strings_mod.INIT_WROTE_FILES_PROXY_EXTRA.format(
                address=address, public_host=public_host,
            ))

    # The state-machine driver is documented in HLD § Flow. v1
    # implementation lands in a follow-up PR — this PR ships the
    # config-write + idempotence path, the library code in up.py /
    # status.py, and unit tests. The integration that wires the
    # state machine to actual `docker compose up -d` + polling is
    # gated on the release-infrastructure PR landing.
    compose_path = paths.docker_compose_yml_path(cfg)
    # On the idempotent fast-path the persisted mode is the source of
    # truth — the operator may have invoked `greffer up` with the default
    # --mode tunnel on a host that was originally initialized as proxy.
    # Print the hint that matches what's on disk.
    from . import env_file as env_mod
    persisted_mode = env_mod.EnvFile.read(paths.env_env_path(cfg)).get("GREFFER_MODE")
    effective_mode = persisted_mode or mode
    if effective_mode == "tunnel":
        manual_hint = f"docker compose -f {compose_path} --profile tunnel up -d"
    else:
        manual_hint = f"docker compose -f {compose_path} up -d"
    typer.echo(
        "(state-machine driver lands in a follow-up PR — this PR ships\n"
        "the package + config write + doctor + status. To bring up the\n"
        f"container manually now: {manual_hint})"
    )


@app.command()
def status(
    config_dir: Optional[str] = typer.Option(
        None, "--config-dir", help="Override the default ~/.greffer/ location",
    ),
) -> None:
    """Read-only status report."""
    cfg = paths.resolve_config_dir(config_dir)
    report = status_mod.collect(cfg)
    print(status_mod.format_report(report))


def _compose_template_path() -> Path:
    """Resolve the bundled compose template (single file, both modes via profiles).

    Mode selection happens at ``docker compose up`` time via the
    ``--profile tunnel`` flag, NOT at template-render time. This mirrors
    the existing in-repo greffer/docker-compose.yml shape — one source
    of truth, two run paths.
    """
    # importlib.resources so the lookup works in both Poetry editable
    # install and (eventually) PyInstaller bundle.
    files = resources.files("greffer_cli").joinpath("templates")
    target = files.joinpath("compose.yml")
    if not target.is_file():
        raise RuntimeError("compose.yml template not bundled (package layout bug)")
    return Path(str(target))


def _read_image_tag() -> str:
    """Read the CLI's pinned greffer image tag from cli/IMAGE_TAG.

    In a built PyInstaller binary, the tag is interpolated into the
    template at bundle time by cli-release.yml (see HLD § "Image-tag
    bundling contract"). In dev (`poetry run greffer up`), we read it
    fresh from the file each time.
    """
    # cli/IMAGE_TAG is one level up from the greffer_cli/ package.
    image_tag_path = Path(__file__).resolve().parent.parent / "IMAGE_TAG"
    if image_tag_path.exists():
        return image_tag_path.read_text(encoding="utf-8").strip()
    return "main"  # dev fallback


if __name__ == "__main__":  # pragma: no cover
    app()
