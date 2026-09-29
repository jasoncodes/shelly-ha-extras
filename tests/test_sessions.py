import pytest

from shelly_ha_extras import sessions
from shelly_ha_extras.config import Config
from shelly_ha_extras.models import Device
from shelly_ha_extras.mqtt import MQTTBridge, topics


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


@pytest.mark.asyncio
async def test_rpc_failure_marks_device_offline_and_discovery_keeps_it_offline(monkeypatch):
    device = Device("test-device", "Test", "Mini1PMG3", "shellyplus1", "1.0")
    client = FakeMQTT()
    bridge = MQTTBridge(Config(), client=client)
    bridge.publish_device(device)
    session = sessions.ShellySession(
        device,
        "192.0.2.10",
        username="admin",
        password=None,
        on_snapshot=bridge.publish_schedules,
        on_connection_change=bridge.set_device_connected,
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

    async def stop_after_failure(_seconds):
        session._stop.set()

    monkeypatch.setattr(sessions, "ShellyRPC", FakeRPC)
    monkeypatch.setattr(sessions, "list_schedules", fail_schedules)
    monkeypatch.setattr(sessions.asyncio, "sleep", stop_after_failure)

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
