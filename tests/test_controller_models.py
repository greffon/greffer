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
    assert req.configurations is None
    assert req.ports is None


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
    but rejects explicit null. Locked in by a field validator."""
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


def test_start_request_missing_configurations_ok() -> None:
    """Field omitted entirely → default None, no validator error."""
    req = GreffonStartRequest.model_validate(
        {"id": "x", "repository_url": "u", "cert": SAMPLE_CERT}
    )
    assert req.configurations is None
    assert req.ports is None


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
