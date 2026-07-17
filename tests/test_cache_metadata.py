import io
import zipfile
from pathlib import Path

import pytest

from shelly_ha_extras.cache import cache_firmware, validate_zip
from shelly_ha_extras.metadata import stable_update, version_key
from shelly_ha_extras.models import IntegrityError, UpdateInfo


class Response:
    def __init__(self, data: bytes, length=None):
        self.data = data
        self.headers = {} if length is None else {"content-length": str(length)}

    def iter_bytes(self):
        yield self.data[:3]
        yield self.data[3:]


def zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", "{}")
    return stream.getvalue()


def test_atomic_cache_and_integrity(tmp_path: Path):
    data = zip_bytes()
    import hashlib

    info = UpdateInfo(
        "app",
        "1.2.0",
        "https://updates.shelly.cloud/app.zip",
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    path = cache_firmware(tmp_path, info, Response(data))
    assert path.read_bytes() == data
    assert validate_zip(path, expected_size=len(data), expected_sha256=info.sha256)


def test_cache_rejects_content_length_and_bad_zip(tmp_path: Path):
    info = UpdateInfo("app", "1.2.0", "https://updates.shelly.cloud/app.zip")
    with pytest.raises(IntegrityError):
        cache_firmware(tmp_path, info, Response(b"not zip", length=99))


def test_metadata_only_selects_stable_and_version_does_not_downgrade():
    payload = {
        "updates": [
            {"version": "1.9.0", "channel": "beta", "url": "https://updates.shelly.cloud/b.zip"},
            {"version": "1.10.0", "channel": "stable", "url": "https://updates.shelly.cloud/s.zip"},
        ]
    }
    result = stable_update(payload, "app")
    assert result and result.version == "1.10.0"
    assert version_key("1.10.0") > version_key("1.9.0")


def test_shelly_metadata_mapping_shape_accepts_official_cdn_without_zip_suffix():
    result = stable_update(
        {
            "beta": {"version": "2.0.0-beta3", "url": "https://fwcdn.shelly.cloud/beta"},
            "stable": {"version": "1.7.5", "url": "https://fwcdn.shelly.cloud/stable"},
        },
        "S2PMG3",
    )
    assert result and result.version == "1.7.5"
