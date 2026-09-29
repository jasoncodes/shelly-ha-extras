from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .cache import cache_firmware, cache_path, validate_zip
from .config import Config
from .device import get_device_info, install
from .discovery import DiscoveredDevice, DiscoveryWatcher, connection_target
from .http import ShellyHTTP
from .metadata import fetch_update, require_official
from .models import Device, KnownDevice, UpdateInfo
from .schedule import ScheduleSnapshot, replace_time, simple_time
from .sessions import SessionManager

INSTALL_COMMAND = '{"version":"latest"}'
LOGGER = logging.getLogger(__name__)


def device_key(device: Device) -> str:
    key = device.id or device.name
    if not key or any(c in key for c in "/+#\x00"):
        raise ValueError(f"device id is not safe for MQTT topics: {key!r}")
    return key


def topics(config: Config, device: Device) -> dict[str, str]:
    key = device_key(device)
    prefix = config.mqtt.topic_prefix.rstrip("/")
    return {
        "state": f"{prefix}/{key}/firmware/state",
        "availability": f"{prefix}/{key}/availability",
        "command": f"{prefix}/{key}/firmware/install",
        "lwt": f"{prefix}/availability",
        "discovery": f"{config.mqtt.discovery_prefix}/update/{key}/config",
        "schedules_state": f"{prefix}/{key}/schedules/state",
        "schedules_update": f"{prefix}/{key}/schedules/update",
    }


def schedule_topics(config: Config, device: Device, job_id: int) -> dict[str, str]:
    key = device_key(device)
    prefix = config.mqtt.topic_prefix.rstrip("/")
    return {
        "state": f"{prefix}/{key}/schedules/{job_id}/state",
        "time_state": f"{prefix}/{key}/schedules/{job_id}/time/state",
        "time_set": f"{prefix}/{key}/schedules/{job_id}/time/set",
        "time_availability": f"{prefix}/{key}/schedules/{job_id}/time/availability",
    }


def state_payload(
    device: Device,
    latest: UpdateInfo | None = None,
    *,
    in_progress: bool = False,
    percentage: float = 0.0,
) -> dict[str, Any]:
    return {
        "installed_version": device.version,
        "latest_version": latest.version if latest else device.version,
        "in_progress": in_progress,
        "update_percentage": round(percentage),
    }


def discovery_payload(config: Config, device: Device) -> dict[str, Any]:
    t = topics(config, device)
    return {
        "platform": "update",
        "name": f"{device.name} Firmware",
        "unique_id": f"{device.id}-firmware",
        "default_entity_id": f"update.{device_key(device).lower()}_firmware",
        "object_id": f"{device_key(device)}_firmware",
        "device": {
            "identifiers": [device.id],
            "name": device.name,
            "model": device.model,
            "manufacturer": "Shelly",
            "sw_version": device.version,
            **({"connections": [["mac", device.mac]]} if device.mac else {}),
        },
        "state_topic": t["state"],
        "command_topic": t["command"],
        "payload_install": INSTALL_COMMAND,
        "availability": [
            {"topic": t["lwt"]},
            {"topic": t["availability"]},
        ],
        "payload_available": "online",
        "payload_not_available": "offline",
        "availability_mode": "all",
        "json_attributes_topic": t["state"],
        "device_class": "firmware",
        "entity_category": "config",
    }


def schedule_discovery_payload(
    config: Config, device: Device, job: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    job_id = int(job["id"])
    entity_key = device_key(device).lower()
    t = schedule_topics(config, device, job_id)
    simple = simple_time(str(job.get("timespec", "")))
    root = f"{config.mqtt.discovery_prefix}/"
    availability: list[dict[str, str]] = [
        {"topic": topics(config, device)["lwt"]},
        {"topic": topics(config, device)["availability"]},
    ]
    base: dict[str, Any] = {
        "device": {
            "identifiers": [device.id],
            "name": device.name,
            "model": device.model,
            "manufacturer": "Shelly",
            **({"connections": [["mac", device.mac]]} if device.mac else {}),
        },
        "availability": availability,
        "availability_mode": "all",
    }
    action_name = ""
    for call in job.get("calls", []):
        if isinstance(call, dict) and call.get("method") == "switch.set":
            params = call.get("params", {})
            if isinstance(params, dict) and "id" in params and "on" in params:
                action_name = (
                    f" (Output {int(params['id']) + 1} {'on' if params['on'] else 'off'})"
                )
                break
    enable = {
        **base,
        "platform": "switch",
        "name": f"Schedule {job_id}{action_name} enabled",
        "unique_id": f"{device.id}-schedule-{job_id}-enable",
        "default_entity_id": f"switch.{entity_key}_schedule_{job_id}_enable",
        "state_topic": t["state"],
        "value_template": "{{ 'ON' if value_json.enable else 'OFF' }}",
        "command_topic": topics(config, device)["schedules_update"],
        "command_template": f'{{{{ {{"id": {job_id}, "enable": value == "ON"}} | to_json }}}}',
        "payload_on": "ON",
        "payload_off": "OFF",
    }
    timespec = {
        **base,
        "platform": "text",
        "name": f"Schedule {job_id}{action_name} timespec",
        "unique_id": f"{device.id}-schedule-{job_id}-timespec",
        "default_entity_id": f"text.{entity_key}_schedule_{job_id}_timespec",
        "enabled_by_default": simple is None,
        "state_topic": t["state"],
        "value_template": "{{ value_json.timespec }}",
        "command_topic": topics(config, device)["schedules_update"],
        "command_template": f'{{{{ {{"id": {job_id}, "timespec": value}} | to_json }}}}',
        "mode": "text",
    }
    time_entity = {
        **base,
        "platform": "time",
        "availability": availability + [{"topic": t["time_availability"]}],
        "name": f"Schedule {job_id}{action_name} time",
        "unique_id": f"{device.id}-schedule-{job_id}-time",
        "default_entity_id": f"time.{entity_key}_schedule_{job_id}",
        "enabled_by_default": simple is not None,
        "state_topic": t["time_state"],
        "command_topic": t["time_set"],
        "device_class": "timestamp",
        "value_template": "{{ value }}",
    }
    return {
        root + f"switch/{device.id}_schedule_{job_id}/config": enable,
        root + f"text/{device.id}_schedule_{job_id}_timespec/config": timespec,
        root + f"time/{device.id}_schedule_{job_id}/config": time_entity,
    }


class KnownDeviceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.devices: dict[str, KnownDevice] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text())
        for key, item in payload.items():
            raw = item["device"]
            device = Device(**raw)
            self.devices[key] = KnownDevice(
                device,
                datetime.fromisoformat(item["first_seen"]),
                datetime.fromisoformat(item["last_seen"]),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: {
                "device": asdict(value.device),
                "first_seen": value.first_seen.isoformat(),
                "last_seen": value.last_seen.isoformat(),
            }
            for key, value in self.devices.items()
        }
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def observe(self, device: Device, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        key = device_key(device)
        if key in self.devices:
            known = self.devices[key]
            known.device = device
            known.last_seen = now
        else:
            self.devices[key] = KnownDevice(device, now, now)

    def cleanup(self, days: float, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        removed = [key for key, item in self.devices.items() if item.last_seen < cutoff]
        for key in removed:
            del self.devices[key]
        return removed


def _advertisement_key(item: DiscoveredDevice | str) -> tuple[Any, ...]:
    if not isinstance(item, DiscoveredDevice):
        return ("target", str(item))
    return (
        "mdns",
        item.addresses,
        item.port,
        tuple(sorted(item.properties.items())),
    )


def _merge_advertisements(
    primary: list[DiscoveredDevice], fallback: list[DiscoveredDevice]
) -> list[DiscoveredDevice]:
    """Add fallback discoveries without discarding the watcher's live snapshot."""
    merged = list(primary)
    known = {_advertisement_key(item) for item in merged}
    for item in fallback:
        key = _advertisement_key(item)
        if key not in known:
            merged.append(item)
            known.add(key)
    return merged


class AdvertisementFailures:
    """Track advertisements needing a retry on the next independent scan."""

    def __init__(self) -> None:
        self._failed: set[tuple[Any, ...]] = set()

    def reconcile(self, discovered: Sequence[DiscoveredDevice | str]) -> None:
        current = {_advertisement_key(item) for item in discovered}
        self._failed.intersection_update(current)

    def should_attempt(self, item: DiscoveredDevice | str) -> bool:
        return _advertisement_key(item) not in self._failed

    def has_failed(self, discovered: Sequence[DiscoveredDevice | str]) -> bool:
        return any(_advertisement_key(item) in self._failed for item in discovered)

    def clear(self, item: DiscoveredDevice | str) -> None:
        self._failed.discard(_advertisement_key(item))

    def record_failure(self, item: DiscoveredDevice | str) -> None:
        self._failed.add(_advertisement_key(item))


class MQTTBridge:
    """MQTT/Home Assistant publisher; the client is injectable for tests."""

    def __init__(self, config: Config, *, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
        else:
            import paho.mqtt.client as mqtt

            self.client = mqtt.Client(client_id=config.mqtt.client_id)
            if config.mqtt.username:
                self.client.username_pw_set(config.mqtt.username, config.mqtt.password)
        self.devices: dict[str, Device] = {}
        self.latest: dict[str, UpdateInfo | None] = {}
        self.connected: dict[str, bool] = {}
        self.command_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.command_event = threading.Event()
        self.schedules: dict[str, ScheduleSnapshot] = {}
        self.schedule_jobs: dict[str, set[int]] = {}

    def connect(self, *, publish_online: bool = True) -> None:
        availability = f"{self.config.mqtt.topic_prefix.rstrip('/')}/availability"
        self.client.will_set(availability, "offline", retain=True)
        self.client.on_message = self._on_message
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        LOGGER.info("connecting to MQTT broker %s:%s", self.config.mqtt.host, self.config.mqtt.port)
        self.client.connect(self.config.mqtt.host, self.config.mqtt.port)
        self.client.loop_start()
        self._subscribe()
        if publish_online:
            self.online()
        LOGGER.info("connected to MQTT broker; service availability topic is %s", availability)

    def online(self) -> None:
        LOGGER.debug("publishing service availability online")
        self.client.publish(
            f"{self.config.mqtt.topic_prefix.rstrip('/')}/availability", "online", retain=True
        )

    def mark_offline(self, device: Device) -> None:
        LOGGER.debug("publishing device availability offline for %s", device_key(device))
        self.connected[device_key(device)] = False
        t = topics(self.config, device)
        self.client.publish(t["availability"], "offline", retain=True)
        self.client.publish(
            t["state"],
            json.dumps(state_payload(device, self.latest.get(device_key(device)))),
            retain=True,
        )

    def set_device_connected(self, device: Device, connected: bool) -> None:
        key = device_key(device)
        if self.connected.get(key) == connected:
            return
        self.connected[key] = connected
        self.client.publish(
            topics(self.config, device)["availability"],
            "online" if connected else "offline",
            retain=True,
        )

    def publish_device(
        self,
        device: Device,
        latest: UpdateInfo | None = None,
        *,
        in_progress: bool = False,
        percentage: float = 0.0,
        available: bool = True,
    ) -> None:
        LOGGER.debug("publishing device %s (%s)", device.id, device.name)
        self.devices[device_key(device)] = device
        self.latest[device_key(device)] = latest
        t = topics(self.config, device)
        self.client.publish(
            t["discovery"], json.dumps(discovery_payload(self.config, device)), retain=True
        )
        self.client.publish(
            t["availability"],
            "online" if available and self.connected.get(device_key(device), True) else "offline",
            retain=True,
        )
        self.client.publish(
            t["state"],
            json.dumps(
                state_payload(
                    device,
                    latest,
                    in_progress=in_progress,
                    percentage=percentage,
                )
            ),
            retain=True,
        )

    def birth(self, devices: list[Device]) -> None:
        self.client.publish(
            f"{self.config.mqtt.topic_prefix.rstrip('/')}/availability", "online", retain=True
        )
        for device in devices:
            self.publish_device(device, self.latest.get(device_key(device)))

    def publish_schedules(self, device: Device, snapshot: ScheduleSnapshot) -> None:
        key = device_key(device)
        t = topics(self.config, device)
        previous = self.schedule_jobs.get(key, set())
        current = {int(job["id"]) for job in snapshot.jobs if "id" in job}
        LOGGER.debug("publishing %d schedules for device %s", len(current), key)
        for removed in previous - current:
            old = schedule_topics(self.config, device, removed)
            for topic in old.values():
                self.client.publish(topic, "", retain=True)
            for topic in (
                f"{self.config.mqtt.discovery_prefix}/switch/{device.id}_schedule_{removed}/config",
                f"{self.config.mqtt.discovery_prefix}/text/{device.id}_schedule_{removed}_timespec/config",
                f"{self.config.mqtt.discovery_prefix}/time/{device.id}_schedule_{removed}/config",
            ):
                self.client.publish(topic, "", retain=True)
        self.schedule_jobs[key] = current
        self.schedules[key] = snapshot
        self.client.publish(t["schedules_state"], json.dumps(snapshot.as_dict()), retain=True)
        for job in snapshot.jobs:
            if "id" not in job:
                continue
            job_id = int(job["id"])
            st = schedule_topics(self.config, device, job_id)
            self.client.publish(st["state"], json.dumps(job), retain=True)
            value = simple_time(str(job.get("timespec", "")))
            self.client.publish(
                st["time_availability"], "online" if value else "offline", retain=True
            )
            if value:
                self.client.publish(
                    st["time_state"], f"{value[0]:02d}:{value[1]:02d}:{value[2]:02d}", retain=True
                )
            for topic, payload in schedule_discovery_payload(self.config, device, job).items():
                self.client.publish(topic, json.dumps(payload), retain=True)

    def close(self) -> None:
        LOGGER.info("stopping MQTT bridge")
        LOGGER.debug("publishing service availability offline")
        self.client.publish(
            f"{self.config.mqtt.topic_prefix.rstrip('/')}/availability",
            "offline",
            retain=True,
        )
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _subscribe(self) -> None:
        if hasattr(self.client, "subscribe"):
            self.client.subscribe(f"{self.config.mqtt.topic_prefix.rstrip('/')}/+/firmware/install")
            self.client.subscribe(f"{self.config.mqtt.topic_prefix.rstrip('/')}/+/schedules/update")
            self.client.subscribe(f"{self.config.mqtt.topic_prefix.rstrip('/')}/+/schedules/+/time/set")
            self.client.subscribe("homeassistant/status")
            LOGGER.info("subscribed to firmware, schedule, and Home Assistant birth topics")

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, rc: Any, *args: Any) -> None:
        self._subscribe()
        LOGGER.info("MQTT connection established (rc=%s)", rc)

    @staticmethod
    def _on_disconnect(_client: Any, _userdata: Any, rc: Any, *args: Any) -> None:
        LOGGER.warning("MQTT connection lost (rc=%s); broker LWT should mark service offline", rc)

    @staticmethod
    def is_install_command(payload: str) -> bool:
        try:
            return cast(object, json.loads(payload)) == {"version": "latest"}
        except json.JSONDecodeError:
            return False

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        payload = (
            message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
        )
        LOGGER.debug("received MQTT command on %s: %s", message.topic, payload)
        self.command_queue.put((str(message.topic), payload))
        self.command_event.set()


def serve_forever(config: Config, scanner: Callable[[], Any]) -> None:
    """Run MQTT discovery and process only explicit install command messages."""
    import asyncio

    bridge = MQTTBridge(config)
    watcher = DiscoveryWatcher()
    watcher.start()
    store = KnownDeviceStore(config.data_directory / "known-devices.json")
    store.load()
    LOGGER.info("loaded %d persisted devices", len(store.devices))
    bridge.connect(publish_online=False)
    for known in store.devices.values():
        bridge.mark_offline(known.device)
    bridge.online()
    http = ShellyHTTP(config.tls)
    known_targets: dict[str, str] = {}
    sessions = SessionManager(
        username=config.auth.username,
        password_for=config.password_for,
        on_snapshot=bridge.publish_schedules,
        on_connection_change=bridge.set_device_connected,
    )
    active_updates: set[str] = set()
    active_updates_lock = threading.Lock()
    completed_updates: queue.SimpleQueue[tuple[str, str, Device]] = queue.SimpleQueue()

    def run_firmware_update(key: str, target: str, device: Device) -> None:
        worker_http = ShellyHTTP(config.tls)
        info: UpdateInfo | None = None
        try:
            info = fetch_update(worker_http, device.app, device.version)
            if info is None:
                LOGGER.info("device %s is already on the latest firmware", key)
                return
            require_official(info)
            LOGGER.info("starting firmware update for %s to %s", key, info.version)
            bridge.publish_device(device, info, in_progress=True, percentage=0)
            path = cache_path(config.cache.directory, info)
            if path.exists():
                validate_zip(path, expected_size=info.size, expected_sha256=info.sha256)
            else:
                response = worker_http.stream(info.url)
                try:
                    path = cache_firmware(config.cache.directory, info, response)
                finally:
                    response.close()
            data = path.read_bytes()
            last_percentage = 0

            async def progress(done: int, size: int) -> None:
                nonlocal last_percentage
                percentage = round(done / size * 100) if size else 100
                if percentage == last_percentage:
                    return
                last_percentage = percentage
                bridge.publish_device(device, info, in_progress=True, percentage=percentage)

            result = asyncio.run(
                install(
                    target,
                    data,
                    device.id,
                    username=config.auth.username,
                    password=config.password_for(device.id),
                    progress=progress,
                    expected_version=info.version,
                    verification_seconds=config.polling.verification_seconds,
                )
            )
            bridge.publish_device(result, info)
            LOGGER.info("firmware update completed for %s at %s", key, result.version)
            completed_updates.put((key, target, result))
            bridge.command_event.set()
        except Exception as exc:
            LOGGER.error("firmware update failed for %s: %s", key, exc)
            if info is not None:
                bridge.publish_device(device, info)
        finally:
            with active_updates_lock:
                active_updates.discard(key)

    try:
        startup_discovered = asyncio.run(scanner())
        LOGGER.info("startup discovery found %d service advertisements", len(startup_discovered))
        initial_discovered: list[DiscoveredDevice] | None = startup_discovered
        last_signature: tuple[Any, ...] | None = None
        advertisement_failures = AdvertisementFailures()
        next_probe = time.monotonic() + config.discovery.probe_seconds
        while True:
            command_wakeup = bridge.command_event.is_set()
            periodic_probe = False
            startup_cycle = initial_discovered is not None
            if command_wakeup:
                bridge.command_event.clear()
                discovered = []
                LOGGER.debug("processing queued MQTT commands before discovery")
            elif initial_discovered is not None:
                discovered = initial_discovered
                initial_discovered = None
            else:
                wait_seconds = min(
                    config.discovery.interval_seconds,
                    max(0.0, next_probe - time.monotonic()),
                    1.0,
                )
                watcher.wait(wait_seconds)
                discovered = watcher.snapshot()
                if time.monotonic() >= next_probe:
                    next_probe = time.monotonic() + config.discovery.probe_seconds
                    try:
                        scanned = asyncio.run(scanner())
                    except Exception as exc:
                        LOGGER.debug("periodic discovery probe failed: %s", exc)
                    else:
                        periodic_probe = True
                        scanned = [item for item in scanned if isinstance(item, DiscoveredDevice)]
                        watcher.reconcile_scan(scanned)
                        discovered = _merge_advertisements(
                            watcher.snapshot(), scanned
                        )
                        LOGGER.debug(
                            "periodic discovery probe found %d advertisements", len(scanned)
                        )
            signature = tuple(
                sorted(
                    repr(item)
                    for item in discovered
                )
            )
            command_wakeup = command_wakeup or bridge.command_event.is_set()
            unchanged = not command_wakeup and not startup_cycle and signature == last_signature
            retry_only = (
                unchanged and periodic_probe and advertisement_failures.has_failed(discovered)
            )
            if unchanged and not retry_only:
                continue
            if not command_wakeup:
                last_signature = signature
            if retry_only:
                LOGGER.debug("retrying failed advertisements after periodic probe")
            else:
                LOGGER.info("discovery cycle found %d service advertisements", len(discovered))
            if not command_wakeup:
                advertisement_failures.reconcile(discovered)
            devices: list[Device] = []
            seen_ids: set[str] = set()
            seen_advertisements: set[tuple[Any, ...]] = set()
            for item in discovered:
                target = (
                    connection_target(item) if isinstance(item, DiscoveredDevice) else str(item)
                )
                if isinstance(item, DiscoveredDevice):
                    advertisement = _advertisement_key(item)
                    if advertisement in seen_advertisements:
                        continue
                    seen_advertisements.add(advertisement)
                if retry_only and advertisement_failures.should_attempt(item):
                    continue
                if not retry_only and not advertisement_failures.should_attempt(item):
                    LOGGER.debug("skipping failed unchanged advertisement for %s", target)
                    continue
                try:
                    device = asyncio.run(
                        get_device_info(
                            target,
                            username=config.auth.username,
                            password=config.password_for(target),
                        )
                    )
                    device_id = device_key(device)
                    if device_id in seen_ids:
                        continue
                    seen_ids.add(device_id)
                    latest = fetch_update(http, device.app, device.version)
                except Exception as exc:
                    LOGGER.warning("could not refresh device %s: %s", target, exc)
                    advertisement_failures.record_failure(item)
                    continue
                advertisement_failures.clear(item)
                devices.append(device)
                LOGGER.info("found device %s at %s", device.id, target)
                known_targets[device_key(device)] = target
                store.observe(device)
                bridge.publish_device(device, latest)
                sessions.ensure(device, target, config.discovery.interval_seconds)
            if not command_wakeup and not retry_only:
                for stale_id in set(sessions.sessions) - seen_ids:
                    LOGGER.info("removing RPC session for disappeared device %s", stale_id)
                    stale_session = sessions.sessions.pop(stale_id)
                    bridge.mark_offline(stale_session.device)
                    stale_session.stop()
            if not retry_only:
                bridge.birth(devices)
                LOGGER.info("published %d active devices", len(devices))

            while True:
                try:
                    key, target, result = completed_updates.get_nowait()
                except queue.Empty:
                    break
                known_targets[key] = target
                store.observe(result)
                sessions.ensure(
                    result, target, config.discovery.interval_seconds, reconnect=True
                )

            while True:
                try:
                    topic, payload = bridge.command_queue.get_nowait()
                except queue.Empty:
                    break
                LOGGER.debug("processing MQTT command on %s", topic)
                if topic == "homeassistant/status":
                    bridge.birth(devices)
                    continue
                if not bridge.is_install_command(payload):
                    parts = topic.split("/")
                    prefix = config.mqtt.topic_prefix.rstrip("/").split("/")
                    if (
                        len(parts) >= len(prefix) + 3
                        and parts[-1] == "update"
                        and parts[-2] == "schedules"
                    ):
                        key = parts[-3]
                        schedule_target = known_targets.get(key)
                        schedule_device = bridge.devices.get(key)
                        if schedule_target and schedule_device:
                            try:
                                params = json.loads(payload)
                                job_id = int(params["id"])
                                allowed = {"id", "enable", "timespec"}
                                if set(params) - allowed or not (
                                    "enable" in params or "timespec" in params
                                ):
                                    raise ValueError("invalid schedule update payload")

                                sessions.sessions[key].update(
                                    job_id,
                                    enable=params.get("enable"),
                                    timespec=params.get("timespec"),
                                )
                                LOGGER.info("schedule update completed for %s job %s", key, job_id)
                            except Exception as exc:
                                LOGGER.error("schedule update failed for %s: %s", key, exc)
                                if key in bridge.schedules:
                                    bridge.publish_schedules(schedule_device, bridge.schedules[key])
                        continue
                    if (
                        len(parts) >= len(prefix) + 5
                        and parts[-1] == "set"
                        and parts[-2] == "time"
                        and parts[-4] == "schedules"
                    ):
                        key = parts[-5]
                        schedule_target = known_targets.get(key)
                        schedule_device = bridge.devices.get(key)
                        if schedule_target and schedule_device:
                            try:
                                job_id = int(parts[-3])
                                snapshot = bridge.schedules[key]
                                job = next(
                                    job
                                    for job in snapshot.jobs
                                    if int(job.get("id", -1)) == job_id
                                )
                                timespec = replace_time(str(job["timespec"]), payload)
                                sessions.sessions[key].update(job_id, timespec=timespec)
                                LOGGER.info(
                                    "schedule time update completed for %s job %s", key, job_id
                                )
                            except Exception as exc:
                                LOGGER.error("schedule time update failed for %s: %s", key, exc)
                                if key in bridge.schedules:
                                    bridge.publish_schedules(schedule_device, bridge.schedules[key])
                        continue
                    continue
                key = topic.split("/")[-3] if topic.endswith("/firmware/install") else ""
                command_target = known_targets.get(key)
                command_device = bridge.devices.get(key)
                if not command_target or not command_device:
                    LOGGER.warning("firmware update target %s is not currently known", key)
                    continue
                with active_updates_lock:
                    if key in active_updates:
                        LOGGER.warning("firmware update already in progress for %s", key)
                        continue
                    active_updates.add(key)
                threading.Thread(
                    target=run_firmware_update,
                    args=(key, command_target, command_device),
                    name=f"firmware-{key}",
                    daemon=True,
                ).start()
            expired = store.cleanup(config.discovery.stale_cleanup_days)
            if expired:
                LOGGER.info("expired %d devices: %s", len(expired), ", ".join(expired))
            store.save()
            if bridge.command_event.wait(
                min(config.discovery.interval_seconds, config.discovery.probe_seconds)
            ):
                LOGGER.debug("waking discovery loop for queued MQTT command")
    finally:
        sessions.stop()
        watcher.close()
        bridge.close()
