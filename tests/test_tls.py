import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from libby_mcp.server import ssl_context


def ca_pem() -> bytes:
    """A throwaway self-signed CA, standing in for a proxy's interception CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Proxy CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def ca_count() -> int:
    return ssl_context().cert_store_stats()["x509_ca"]


def test_public_roots_without_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    assert ca_count() > 100


def test_ssl_cert_file_adds_to_public_roots(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    public = ca_count()

    bundle = tmp_path / "proxy.pem"
    bundle.write_bytes(ca_pem())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))

    assert ca_count() == public + 1


def test_missing_paths_are_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing"))
    assert ca_count() > 100
