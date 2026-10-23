from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.controller import (
    Certificate,
    GreffonStartRequest,
    GreffonStopRequest,
)


SAMPLE_CERT = {
    "certificate": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----",
}


def test_certificate_valid() -> None:
    c = Certificate(**SAMPLE_CERT)
    assert c.certificate.startswith("-----BEGIN CERTIFICATE")
    assert c.private_key.startswith("-----BEGIN RSA PRIVATE KEY")


def test_certificate_missing_certificate() -> None:
    with pytest.raises(ValidationError):
        Certificate(private_key="k")  # type: ignore[call-arg]


def test_start_request_minimal_valid() -> None:
    req = GreffonStartRequest(
        id="test-123",
        repository_url="https://example.com/docker-compose.yml",
        cert=Certificate(**SAMPLE_CERT),
    )
    assert req.id == "test-123"
    # Omitted fields default to empty containers (not None) so the dumped
    # dict always has the keys present for downstream strict-access code.
    assert req.configurations == []
    assert req.ports == {}


def test_start_request_full_payload() -> None:
    req = GreffonStartRequest(
        id="test-123",
        repository_url="https://example.com/compose.yml",
        cert=SAMPLE_CERT,  # type: ignore[arg-type]
        configurations=[
            {"value": {"db_host": "localhost"}, "destinations": [{"type": "json"}]}
        ],
        ports={"app_80": {"url": "https://example.greffon.io"}},
    )
    assert req.configurations is not None
    assert len(req.configurations) == 1
    assert req.ports == {"app_80": {"url": "https://example.greffon.io"}}


def test_start_request_missing_id() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest(  # type: ignore[call-arg]
            repository_url="https://example.com/compose.yml",
            cert=Certificate(**SAMPLE_CERT),
        )


def test_start_request_missing_cert() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest(  # type: ignore[call-arg]
            id="x",
            repository_url="https://example.com/compose.yml",
        )


def test_start_request_missing_repository_url() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest(  # type: ignore[call-arg]
            id="x",
            cert=Certificate(**SAMPLE_CERT),
        )


def test_stop_request_valid() -> None:
    req = GreffonStopRequest(id="test-123")
    assert req.id == "test-123"


def test_stop_request_missing_id() -> None:
    with pytest.raises(ValidationError):
        GreffonStopRequest()  # type: ignore[call-arg]


def test_start_request_explicit_null_compose_yaml_is_catalog() -> None:
    """Explicit null == absent: the wire-compat story for a manager that
    always includes the key."""
    req = GreffonStartRequest(
        id="x",
        repository_url="https://example.com/docker-compose.yml",
        compose_yaml=None,
        cert=Certificate(**SAMPLE_CERT),
    )
    assert req.compose_yaml is None
    assert req.repository_url.startswith("https://")


def test_start_request_inline_compose_without_url() -> None:
    """Custom-app inline transport (foundation Feature 4): compose_yaml
    replaces the URL entirely — repository_url omitted, not empty."""
    req = GreffonStartRequest(
        id="x",
        compose_yaml="services:\n  app:\n    image: nginx\n",
        cert=Certificate(**SAMPLE_CERT),
    )
    assert req.compose_yaml.startswith("services:")
    assert req.repository_url is None


def test_start_request_inline_and_url_both_refused() -> None:
    """Exactly one compose source: sending both is contradictory (which
    bytes would run?), so it is refused rather than silently preferring
    one."""
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="x",
            repository_url="https://example.com/compose.yml",
            compose_yaml="services: {}",
            cert=Certificate(**SAMPLE_CERT),
        )


def test_start_request_empty_inline_compose_refused() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="x", compose_yaml="", cert=Certificate(**SAMPLE_CERT)
        )


def test_start_request_oversized_inline_compose_refused() -> None:
    """Same 1 MiB bound as the preflight request: an unbounded body is
    tenant-triggerable memory before any render budget runs."""
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="x", compose_yaml="a" * 1048577,
            cert=Certificate(**SAMPLE_CERT),
        )


def test_start_request_empty_repository_url_still_refused() -> None:
    """The catalog path's min_length floor survives the field becoming
    optional: '' must not reach requests.get('') and 500."""
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="x", repository_url="", cert=Certificate(**SAMPLE_CERT)
        )


def test_start_request_accepts_any_shape_in_ports() -> None:
    """ports is `Any`-typed to match DRF's DictField; anything hashable goes."""
    req = GreffonStartRequest(
        id="x",
        repository_url="u",
        cert=Certificate(**SAMPLE_CERT),
        ports={"arbitrary": [1, 2, 3]},
    )
    assert req.ports == {"arbitrary": [1, 2, 3]}


def test_start_request_accepts_any_shape_in_configurations() -> None:
    """configurations[].value/destinations are `Any`-typed."""
    req = GreffonStartRequest(
        id="x",
        repository_url="u",
        cert=Certificate(**SAMPLE_CERT),
        configurations=[
            {"value": "plain-string", "destinations": ["plain-list"]},
        ],
    )
    assert req.configurations is not None
    assert req.configurations[0].value == "plain-string"
    assert req.configurations[0].destinations == ["plain-list"]


def test_start_request_rejects_explicit_null_configurations() -> None:
    """DRF semantics: `required=False` w/o `allow_null=True` accepts missing
    but rejects explicit null. Pydantic rejects null on type grounds
    because the field is typed ``list[...]`` (not ``list[...] | None``)."""
    with pytest.raises(ValidationError):
        GreffonStartRequest.model_validate(
            {
                "id": "x",
                "repository_url": "u",
                "cert": SAMPLE_CERT,
                "configurations": None,
            }
        )


def test_start_request_rejects_explicit_null_ports() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest.model_validate(
            {
                "id": "x",
                "repository_url": "u",
                "cert": SAMPLE_CERT,
                "ports": None,
            }
        )


def test_start_request_missing_configurations_defaults_to_empty() -> None:
    """Field omitted entirely → default empty container (not None), so the
    dumped dict always carries the key for downstream strict access in
    ``apps/utils/greffon/repository.py:create_greffon_info``."""
    req = GreffonStartRequest.model_validate(
        {"id": "x", "repository_url": "u", "cert": SAMPLE_CERT}
    )
    assert req.configurations == []
    assert req.ports == {}
    dumped = req.model_dump()
    assert dumped["configurations"] == []
    assert dumped["ports"] == {}


def test_start_request_rejects_blank_repository_url() -> None:
    """DRF ``CharField`` rejects blank values by default. Blank here would
    reach ``requests.get('')`` and 500 instead of returning 400."""
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="x",
            repository_url="",
            cert=Certificate(**SAMPLE_CERT),
        )


def test_start_request_id_rejects_empty_string() -> None:
    """Defense-in-depth: empty id path-joins to $GREFFON_PATH root."""
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="",
            repository_url="u",
            cert=Certificate(**SAMPLE_CERT),
        )


def test_stop_request_id_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        GreffonStopRequest(id="")


def test_start_request_id_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError):
        GreffonStartRequest(
            id="../../etc/passwd",
            repository_url="u",
            cert=Certificate(**SAMPLE_CERT),
        )
