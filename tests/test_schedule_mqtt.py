import json

from shelly_ha_extras.config import Config
from shelly_ha_extras.models import Device
from shelly_ha_extras.mqtt import MQTTBridge, schedule_discovery_payload, schedule_topics, topics
from shelly_ha_extras.schedule import ScheduleSnapshot


class FakeMQTT:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


def device() -> Device:
    return Device(
        "ogemray25a-d885ac041e78", "Timer", "SNSW-001P16EU", "Plus1PM", "1.0", mac="AA:BB"
    )


def test_schedule_topics_use_exact_shelly_id() -> None:
    cfg = Config()
    assert topics(cfg, device())["schedules_state"].endswith(
        "/ogemray25a-d885ac041e78/schedules/state"
    )
    assert schedule_topics(cfg, device(), 2)["time_set"].endswith("/schedules/2/time/set")


def test_discovery_has_shared_update_topic_and_three_availabilities() -> None:
    payloads = schedule_discovery_payload(
        Config(),
        device(),
        {
            "id": 2,
            "enable": True,
            "timespec": "0 30 14 * * 0,1,2,3,4,5,6",
            "calls": [{"method": "switch.set", "params": {"id": 0, "on": True}}],
        },
    )
    time_payload = next(value for key, value in payloads.items() if "/time/" in key)
    assert time_payload["platform"] == "time"
    assert time_payload["default_entity_id"] == "time.ogemray25a-d885ac041e78_schedule_2"
    switch_payload = next(value for key, value in payloads.items() if "/switch/" in key)
    assert switch_payload["name"] == "Schedule 2 (Output 1 on) enabled"
    timespec_payload = next(value for key, value in payloads.items() if "/text/" in key)
    assert timespec_payload["enabled_by_default"] is False
    assert time_payload["enabled_by_default"] is True
    assert len(time_payload["availability"]) == 3
    assert any(
        value.get("command_topic", "").endswith("/schedules/update")
        for value in payloads.values()
    )


def test_publish_schedules_tombstones_removed_jobs() -> None:
    client = FakeMQTT()
    bridge = MQTTBridge(Config(), client=client)
    current = ScheduleSnapshot([{"id": 1, "enable": True, "timespec": "0 30 9 * * 0,1,2,3,4,5,6"}])
    bridge.publish_schedules(device(), current)
    client.published.clear()
    bridge.publish_schedules(device(), ScheduleSnapshot([]))
    assert any(
        topic.endswith("/schedules/1/state") and payload == "" and retain
        for topic, payload, retain in client.published
    )
    aggregate = next(
        payload for topic, payload, _ in client.published if topic.endswith("/schedules/state")
    )
    assert json.loads(aggregate) == {"jobs": []}
