from pathlib import Path

import pytest

from shelly_ha_extras.config import TLSConfig
from shelly_ha_extras.models import IntegrityError
from shelly_ha_extras.tls import _ca_pem, ssl_context, validate_url


def test_tls_hostname_is_fail_closed():
    cfg = TLSConfig()
    validate_url("https://updates.shelly.cloud/update/app", cfg)
    with pytest.raises(IntegrityError):
        validate_url("https://example.com/update/app", cfg)


def test_tls_allows_explicit_diagnostic_override():
    assert TLSConfig(insecure_diagnostic=True).insecure_diagnostic


def test_shelly_der_bundle_is_accepted_as_ca():
    path = Path(__file__).parents[1] / "shelly_cloud.pem"
    assert "BEGIN CERTIFICATE" in _ca_pem(path)
    assert ssl_context(TLSConfig(ca_file=path)).verify_mode
