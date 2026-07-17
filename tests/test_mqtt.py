from datetime import UTC, datetime, timedelta
from pathlib import Path

from shelly_ha_extras.config import Config
from shelly_ha_extras.models import Device, UpdateInfo
from shelly_ha_extras.mqtt import (
    INSTALL_COMMAND,
    KnownDeviceStore,
    discovery_payload,
    state_payload,
)


def device() -> Device:
    return Device("shelly1-ABC", "Kitchen", "Mini1PMG3", "shellyplus1", "1.0.0", mac="AA:BB")


def test_home_assistant_payload_has_exact_install_command():
    payload = discovery_payload(Config(), device())
    assert payload["platform"] == "update"
    assert len(payload["availability"]) == 2
    assert payload["availability_mode"] == "all"
    assert payload["payload_install"] == INSTALL_COMMAND
    assert payload["default_entity_id"] == "update.shelly1-abc_firmware"
    assert payload["device"]["model"] == "Mini1PMG3"
    state = state_payload(
        device(),
        UpdateInfo("shellyplus1", "1.1.0", "https://updates.shelly.cloud/a.zip"),
        in_progress=True,
        percentage=50,
    )
    assert state["installed_version"] == "1.0.0"
    assert state["latest_version"] == "1.1.0"
    assert state["update_percentage"] == 50


def test_known_device_cleanup_after_seven_days(tmp_path: Path):
    store = KnownDeviceStore(tmp_path / "known.json")
    old = datetime.now(UTC) - timedelta(days=8)
    store.observe(device(), old)
    store.observe(Device("new", "New", "M", "a", "1"))
    removed = store.cleanup(7)
    assert removed == ["shelly1-ABC"]
    store.save()
    assert (tmp_path / "known.json").exists()
