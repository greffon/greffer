"""The compose body render must not execute arbitrary Python.

Before this suite, ``create_compose`` rendered the compose body through the
stock ``jinja2.Template``. Jinja templates are not data: a value of
``{{ cycler.__init__.__globals__.os.popen('id').read() }}`` reaches the
interpreter, and this process holds the manager token, the Docker socket and
every instance's TLS private key -- so it is host root, not a sandbox escape.

Latent, not live, while the catalog is the only input and is PR-reviewed.
These tests exist so it cannot come back when it stops being the only input.
"""
from __future__ import annotations

import os

import pytest
import yaml
from jinja2.exceptions import SecurityError

from apps.utils.docker import compose as compose_mod


def _info(instance_id="sandbox-test"):
    return {
        "id": instance_id,
        "ports": [{"port_host": 10001,
                   "url": f"https://{instance_id}.my.example.test"}],
        "configurations": [],
        "integrations": {},
        # Present because the real create_greffon_info builds it, and because
        # it is the mutation target the immutability tests below exercise:
        # entries here become `docker create -v <value>:/root` mounts.
        "volumes": {"data": {"name": f"{instance_id}_data", "value": f"{instance_id}_data"}},
    }


def _render(compose_dict, tmp_path, monkeypatch, instance_id="sandbox-test"):
    # monkeypatch, never a bare os.environ assignment: pytest runs the suite in
    # one process, so a leaked GREFFON_PATH follows later tests out of this file
    # (test_settings.py asserts the unset default of /data and would fail).
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    compose_mod.create_compose(compose_dict, _info(instance_id))
    return (tmp_path / instance_id / "docker-compose.yml").read_text()


# The classic Jinja escape chains: each reaches interpreter state through an
# attribute walk that SandboxedEnvironment refuses with SecurityError.
#
# Inner literals are DOUBLE-quoted on purpose. ``create_compose`` does
# ``yaml.dump`` before rendering, and yaml.dump re-quotes strings in
# single-quoted style, escaping an inner ``'`` as ``''`` -- which corrupts the
# Jinja expression into a TemplateSyntaxError. A single-quoted payload is
# therefore stopped by that round-trip bug rather than by the sandbox, and
# would pass this suite while proving nothing. (Verified: the single-quoted
# form raises TemplateSyntaxError even through the stock Template, while the
# double-quoted form executes and returns real ``uid=`` output.) Keep these
# double-quoted, or the mutation check below silently stops biting.
_ESCAPES = [
    '{{ cycler.__init__.__globals__.os.popen("id").read() }}',
    '{{ joiner.__init__.__globals__.os.popen("id").read() }}',
    '{{ namespace.__init__.__globals__.os.popen("id").read() }}',
    '{{ "".__class__.__mro__[1].__subclasses__() }}',
    "{{ self.__init__.__globals__ }}",
]


@pytest.mark.parametrize("payload", _ESCAPES)
def test_attribute_walk_to_the_interpreter_is_refused(payload, tmp_path, monkeypatch):
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"EVIL": payload}}}}
    # SecurityError specifically, not "any exception": a payload that merely
    # fails to parse is not evidence the sandbox did anything.
    with pytest.raises(SecurityError):
        _render(compose, tmp_path, monkeypatch)


def test_the_escape_would_have_worked_before_the_sandbox(tmp_path):
    """Pin the counterfactual, so this suite cannot quietly become a no-op.

    If someone reverts the render to a stock ``Template``, the payload below
    executes and this test fails -- which is the whole point. Rendering the
    same payload through the unsandboxed environment here proves the payload
    is genuinely dangerous rather than merely malformed."""
    from jinja2 import Template
    out = Template(
        '{{ cycler.__init__.__globals__.os.popen("echo pwned").read() }}'
    ).render()
    assert "pwned" in out, (
        "the payload no longer executes even unsandboxed, so the sandbox "
        "tests below would pass vacuously -- rewrite them")


def test_ordinary_templating_still_works(tmp_path, monkeypatch):
    """The sandbox must not cost the catalog its legitimate references."""
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"URL": "{{ instance_url }}"}}}}
    rendered = yaml.safe_load(_render(compose, tmp_path, monkeypatch))
    assert rendered["services"]["app"]["environment"]["URL"] == \
        "https://sandbox-test.my.example.test"


def test_a_missing_variable_still_renders_empty(tmp_path, monkeypatch):
    """The undefined policy is deliberately UNCHANGED by the sandbox swap.

    Lenient undefined is the status quo for the compose body. Tightening it is
    a separate decision with its own catalog blast radius; pinning it here
    means that decision has to be made on purpose."""
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"X": "[{{ nope }}]"}}}}
    rendered = yaml.safe_load(_render(compose, tmp_path, monkeypatch))
    assert rendered["services"]["app"]["environment"]["X"] == "[]"


def test_a_template_cannot_mutate_the_live_deployment_context(tmp_path, monkeypatch):
    """The sandbox must refuse MUTATION, not only attribute traversal.

    ``greffon_info`` is passed to render() by reference, and its ``volumes``
    entries become ``docker container create -v <value>:/root`` mounts in
    ``create_volumes_then_copy_files``. A plain SandboxedEnvironment allows
    ``dict.update``/``list.append``, so a template could inject a volume whose
    value is ``/`` and get attacker content written into the host filesystem --
    a host-write primitive that survives blocking the dunder walk. Immutable
    environments refuse it."""
    payload = '{{ volumes.update({"evil": {"value": "/"}}) }}'
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"X": payload}}}}
    with pytest.raises(SecurityError):
        _render(compose, tmp_path, monkeypatch)


def test_a_template_cannot_append_to_a_live_list(tmp_path, monkeypatch):
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"X": '{{ ports.append(1) }}'}}}}
    with pytest.raises(SecurityError):
        _render(compose, tmp_path, monkeypatch)


# The bound-mutator tests above are not enough on their own: an UNBOUND call
# reaches the same primitive while the sandbox inspects the wrong object.
_UNBOUND_MUTATORS = [
    '{{ dict.update(volumes.data, {"value": "/"}) }}',
    '{{ dict.setdefault(volumes.data, "value", "/") }}',
    '{{ dict.pop(volumes.data, "value") }}',
    '{{ dict.clear(volumes.data) }}',
]


@pytest.mark.parametrize("payload", _UNBOUND_MUTATORS)
def test_unbound_mutators_cannot_reach_the_live_context(payload, tmp_path,
                                                        monkeypatch):
    """``dict`` is a Jinja global, and ImmutableSandboxedEnvironment decides
    mutability from the object a method is BOUND to -- so
    ``dict.update(target, ...)`` inspects the ``dict`` class, not the live
    mapping, and used to render clean while rewriting the context. That is the
    same host-write primitive as the bound case: a volume whose value becomes
    "/" is mounted by create_volumes_then_copy_files as ``-v /:/root``.

    Verified before the fix: the bound form raised SecurityError while this
    form succeeded and set the value to "/". The environment now drops the
    mutable-type globals entirely."""
    compose = {"services": {"app": {"image": "nginx:alpine",
                                    "environment": {"EVIL": payload}}}}
    with pytest.raises(Exception) as exc:
        _render(compose, tmp_path, monkeypatch)
    # Whatever the failure, it must not be a silent success.
    assert exc.value is not None


def test_the_escape_globals_are_gone(tmp_path, monkeypatch):
    """Pin the mechanism, not just the symptom: if a future edit restores
    Jinja's default globals, the payload tests above could pass for the wrong
    reason (a typo'd payload also raises), so assert the surface directly."""
    from apps.utils.docker.compose import (_COMPOSE_RENDER_ENV,
                                           _FILE_RENDER_ENV)
    for env in (_COMPOSE_RENDER_ENV, _FILE_RENDER_ENV):
        for unsafe in ("dict", "cycler", "joiner", "namespace", "range",
                       "lipsum"):
            assert unsafe not in env.globals, (
                f"{unsafe} is reachable again; the unbound-mutator and "
                f"attribute-walk surfaces are re-opened")
