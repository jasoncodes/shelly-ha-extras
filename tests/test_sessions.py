import asyncio

import pytest

from shelly_ha_extras import sessions
from shelly_ha_extras.config import Config
from shelly_ha_extras.models import Device
from shelly_ha_extras.mqtt import MQTTBridge, topics
from shelly_ha_extras.schedule import ScheduleSnapshot


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


def test_mark_offline_republishes_retained_availability():
    device = Device("test-device", "Test", "Mini1PMG3", "shellyplus1", "1.0")
    client = FakeMQTT()
    bridge = MQTTBridge(Config(), client=client)
    bridge.set_device_connected(device, False)
    client.published.clear()

    bridge.mark_offline(device)

    assert client.published[0] == (topics(Config(), device)["availability"], "offline", True)


@pytest.mark.asyncio
async def test_rpc_failure_marks_device_offline_and_discovery_keeps_it_offline(monkeypatch):
    device = Device("test-device", "Test", "Mini1PMG3", "shellyplus1", "1.0")
    client = FakeMQTT()
    bridge = MQTTBridge(Config(), client=client)
    bridge.publish_device(device)

    def on_connection_change(device, connected):
        bridge.set_device_connected(device, connected)
        if not connected:
            session._stop.set()

    session = sessions.ShellySession(
        device,
        "192.0.2.10",
        username="admin",
        password=None,
        on_snapshot=bridge.publish_schedules,
        on_connection_change=on_connection_change,
    )

    class FakeRPC:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def fail_schedules(_rpc):
        raise OSError("device disconnected")

    monkeypatch.setattr(sessions, "ShellyRPC", FakeRPC)
    monkeypatch.setattr(sessions, "list_schedules", fail_schedules)

    await session._run()
    bridge.birth([device])

    availability = topics(Config(), device)["availability"]
    assert [payload for topic, payload, _ in client.published if topic == availability] == [
        "online",
        "online",
        "offline",
        "offline",
    ]

    bridge.set_device_connected(device, True)
    assert client.published[-1] == (availability, "online", True)


@pytest.mark.asyncio
async def test_reconnect_request_interrupts_rpc_backoff(monkeypatch):
    device = Device("test-device", "Test", "Mini1PMG3", "shellyplus1", "1.0")
    changes = []

    def on_connection_change(_device, connected):
        changes.append(connected)
        if connected:
            session._stop.set()
        else:
            session.request_reconnect()

    session = sessions.ShellySession(
        device,
        "192.0.2.10",
        username="admin",
        password=None,
        on_snapshot=lambda *_: None,
        on_connection_change=on_connection_change,
    )

    class FakeRPC:
        attempts = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            FakeRPC.attempts += 1
            if FakeRPC.attempts == 1:
                raise OSError("rebooting")
            return self

        async def __aexit__(self, *args):
            pass

    async def schedules(_rpc):
        return ScheduleSnapshot([])

    monkeypatch.setattr(sessions, "ShellyRPC", FakeRPC)
    monkeypatch.setattr(sessions, "list_schedules", schedules)

    await asyncio.wait_for(session._run(), timeout=0.5)
    assert changes == [False, True, False]
    assert FakeRPC.attempts == 2
