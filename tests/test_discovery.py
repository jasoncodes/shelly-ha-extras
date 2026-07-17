from shelly_ha_extras.discovery import (
    SHELLY_SERVICE_TYPE,
    DiscoveredDevice,
    _decode_txt,
    connection_target,
)
from shelly_ha_extras.mqtt import AdvertisementFailures, _merge_advertisements


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
