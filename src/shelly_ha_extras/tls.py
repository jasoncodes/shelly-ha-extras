from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlparse

from .config import TLSConfig
from .models import IntegrityError


def _der_end(data: bytes, offset: int) -> int:
    if offset + 2 > len(data) or data[offset] != 0x30:
        raise IntegrityError("configured CDN CA does not contain an X.509 certificate")
    first = data[offset + 1]
    if first & 0x80:
        count = first & 0x7F
        start = offset + 2
        end = start + count
        if end > len(data):
            raise IntegrityError("configured Shelly CA has a truncated DER length")
        body_length = int.from_bytes(data[start:end], "big")
        header_length = 2 + count
    else:
        body_length = first
        header_length = 2
    end = offset + header_length + body_length
    if end > len(data):
        raise IntegrityError("configured Shelly CA has a truncated DER certificate")
    return end


def _ca_pem(path: Path) -> str:
    data = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in data:
        return data.decode("ascii")
    if data.startswith(b"DER\n"):
        start = data.find(b"\x30\x82", 4)
        if start < 0:
            raise IntegrityError("configured Shelly CA bundle has no DER certificate")
        data = data[start : _der_end(data, start)]
    elif not data.startswith(b"\x30"):
        raise IntegrityError("configured CDN CA is neither PEM nor DER")
    return ssl.DER_cert_to_PEM_cert(data)


def validate_url(url: str, config: TLSConfig) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in config.allowed_hosts:
        raise IntegrityError("update metadata/download hostname is not an allowed HTTPS CDN host")
    return parsed.hostname


def validate_certificate(host: str, config: TLSConfig) -> None:
    del host
    if config.insecure_diagnostic:
        return
    if not config.ca_file:
        raise IntegrityError("a Shelly CA file is required for CDN TLS")
    _ca_pem(config.ca_file)


def ssl_context(config: TLSConfig) -> ssl.SSLContext:
    if config.insecure_diagnostic:
        return ssl._create_unverified_context()
    if config.ca_file:
        # Shelly's self-signed CA predates X.509 basicConstraints. Recent
        # Python versions enable VERIFY_X509_STRICT in create_default_context,
        # which rejects that CA. A client context keeps certificate and
        # hostname verification enabled while trusting only this supplied CA.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=_ca_pem(config.ca_file))
    else:
        context = ssl.create_default_context()
    return context
