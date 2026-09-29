from types import SimpleNamespace

import pytest

from shelly_ha_extras.discovery import (
    SHELLY_SERVICE_TYPE,
    DiscoveredDevice,
    DiscoveryWatcher,
    _decode_txt,
    connection_target,
)
from shelly_ha_extras.mqtt import AdvertisementFailures, _merge_advertisements


@pytest.mark.parametrize("second_hostname", ["Test-Switch.local.", "Other-Name.local."])
def test_watcher_removes_only_matching_service(monkeypatch, second_hostname):
    service_name = "Shelly1PMMiniG3-48F6EEB92984._shelly._tcp.local."
    alias_name = "Test-Switch._shelly._tcp.local."

    class FakeZeroconf:
        def get_service_info(self, _service_type, _name):
            return SimpleNamespace(
                server=second_hostname if _name == alias_name else "Test-Switch.local.",
                port=80,
                properties={},
                parsed_addresses=lambda: ["192.0.2.10"],
            )

        def close(self):
            pass

    class FakeBrowser:
        def __init__(self, zc, service_type, listener):
            self.listener = listener
            self.zc = zc
            self.service_type = service_type

        def cancel(self):
            pass

    monkeypatch.setattr("zeroconf.Zeroconf", FakeZeroconf)
    monkeypatch.setattr("zeroconf.ServiceBrowser", FakeBrowser)
    watcher = DiscoveryWatcher()
    watcher.start()
    browser = watcher._browser
    browser.listener.add_service(browser.zc, browser.service_type, service_name)
    browser.listener.add_service(browser.zc, browser.service_type, alias_name)
    assert len(watcher.snapshot()) == 2

    browser.listener.remove_service(browser.zc, browser.service_type, service_name)
    assert watcher.snapshot() == [
        DiscoveredDevice(second_hostname.rstrip("."), ("192.0.2.10",), 80, {})
    ]
    browser.listener.remove_service(browser.zc, browser.service_type, alias_name)
    assert watcher.snapshot() == []
    watcher.close()


def test_shelly_specific_service_and_txt_decoding():
    assert SHELLY_SERVICE_TYPE == "_shelly._tcp.local."
    assert _decode_txt(b"gen") == "gen"
    assert _decode_txt(b"3") == "3"


def test_connection_target_prefers_advertised_address():
    device = DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {})
    assert connection_target(device) == "192.0.2.10"


def test_connection_target_brackets_ipv6_address():
    device = DiscoveredDevice("timer.local", ("2001:db8::10",), 80, {})
    assert connection_target(device) == "[2001:db8::10]"


def test_empty_fallback_scan_does_not_discard_watcher_snapshot():
    device = DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {})

    assert _merge_advertisements([device], []) == [device]


def test_fallback_scan_adds_advertisements_missing_from_watcher():
    watched = DiscoveredDevice("watched.local", ("192.0.2.10",), 80, {})
    scanned = DiscoveredDevice("scanned.local", ("192.0.2.11",), 80, {})

    assert _merge_advertisements([watched], [watched, scanned]) == [watched, scanned]


def test_failed_advertisement_is_retried_only_after_disappearing():
    device = DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {})
    failures = AdvertisementFailures()

    failures.record_failure(device)
    failures.reconcile([device])
    assert not failures.should_attempt(device)

    failures.reconcile([])
    failures.reconcile([device])
    assert failures.should_attempt(device)


def test_changed_advertisement_is_retried():
    old = DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {})
    changed = DiscoveredDevice("timer.local", ("192.0.2.12",), 80, {})
    failures = AdvertisementFailures()

    failures.record_failure(old)
    failures.reconcile([changed])

    assert failures.should_attempt(changed)
