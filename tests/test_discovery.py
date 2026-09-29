from types import SimpleNamespace

import pytest
from zeroconf import ServiceStateChange

from shelly_ha_extras.discovery import (
    SHELLY_SERVICE_TYPE,
    DiscoveredDevice,
    DiscoveryWatcher,
    _decode_txt,
    connection_target,
    discover,
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
        DiscoveredDevice(second_hostname.rstrip("."), ("192.0.2.10",), 80, {}, alias_name)
    ]
    browser.listener.remove_service(browser.zc, browser.service_type, alias_name)
    assert watcher.snapshot() == []
    watcher.close()


def test_watcher_retires_services_absent_from_two_scans(monkeypatch):
    service_name = "Shelly1PMMiniG3-48F6EEB93338._shelly._tcp.local."

    class FakeZeroconf:
        def get_service_info(self, _service_type, _name):
            return SimpleNamespace(
                server="Test-Switch.local.",
                port=80,
                properties={},
                parsed_addresses=lambda: ["192.0.2.10"],
            )

        def close(self):
            pass

    class FakeBrowser:
        def __init__(self, zc, service_type, listener):
            self.zc = zc
            self.service_type = service_type
            self.listener = listener

        def cancel(self):
            pass

    monkeypatch.setattr("zeroconf.Zeroconf", FakeZeroconf)
    monkeypatch.setattr("zeroconf.ServiceBrowser", FakeBrowser)
    watcher = DiscoveryWatcher()
    watcher.start()
    browser = watcher._browser
    browser.listener.add_service(browser.zc, browser.service_type, service_name)
    watcher.reconcile_scan([])
    assert len(watcher.snapshot()) == 1
    watcher.reconcile_scan(
        [DiscoveredDevice("Test-Switch.local", ("192.0.2.10",), 80, {}, service_name)]
    )
    watcher.reconcile_scan([])
    assert len(watcher.snapshot()) == 1
    watcher.reconcile_scan([])
    assert watcher.snapshot() == []
    watcher.close()


@pytest.mark.asyncio
async def test_async_scan_accepts_zeroconf_callback_keywords(monkeypatch):
    class FakeZeroconf:
        def __init__(self):
            self.zeroconf = self

        async def async_get_service_info(self, service_type, name):
            assert service_type == SHELLY_SERVICE_TYPE
            assert name in {
                "timer._shelly._tcp.local.",
                "timer-alias._shelly._tcp.local.",
            }
            return SimpleNamespace(
                server="timer.local.",
                port=80,
                properties={b"gen": b"3"},
                parsed_addresses=lambda: ["192.0.2.10"],
            )

        async def async_close(self):
            pass

    class FakeBrowser:
        def __init__(self, zeroconf, service_type, handlers):
            assert service_type == SHELLY_SERVICE_TYPE
            for name in ("timer._shelly._tcp.local.", "timer-alias._shelly._tcp.local."):
                handlers[0](
                    zeroconf=zeroconf,
                    service_type=service_type,
                    name=name,
                    state_change=ServiceStateChange.Added,
                )

        async def async_cancel(self):
            pass

    monkeypatch.setattr("zeroconf.asyncio.AsyncZeroconf", FakeZeroconf)
    monkeypatch.setattr("zeroconf.asyncio.AsyncServiceBrowser", FakeBrowser)

    assert await discover(0.01) == [
        DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {"gen": "3"}, name)
        for name in ("timer._shelly._tcp.local.", "timer-alias._shelly._tcp.local.")
    ]


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


def test_failed_advertisement_clears_after_retry_or_disappearance():
    device = DiscoveredDevice("timer.local", ("192.0.2.10",), 80, {})
    failures = AdvertisementFailures()

    failures.record_failure(device)
    failures.reconcile([device])
    assert not failures.should_attempt(device)
    assert failures.has_failed([device])

    failures.clear(device)
    assert failures.should_attempt(device)
    failures.record_failure(device)

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
