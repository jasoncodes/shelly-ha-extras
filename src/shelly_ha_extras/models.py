from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str
    model: str
    app: str
    version: str
    auth_enforced: bool = False
    mac: str | None = None
    hostname: str | None = None
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    app: str
    version: str
    url: str
    sha256: str | None = None
    size: int | None = None
    channel: str = "stable"


@dataclass(slots=True)
class KnownDevice:
    device: Device
    first_seen: datetime = field(default_factory=utcnow)
    last_seen: datetime = field(default_factory=utcnow)


class ShellyError(Exception):
    """Base exception for actionable updater failures."""


class DiscoveryError(ShellyError):
    pass


class ProtocolError(ShellyError):
    pass


class AuthenticationError(ShellyError):
    pass


class IntegrityError(ShellyError):
    pass


class VersionTimeout(ShellyError):
    pass


def device_from_info(
    info: dict[str, Any], *, hostname: str | None = None, addresses: tuple[str, ...] = ()
) -> Device:
    """Normalize the fields returned by Shelly.GetDeviceInfo."""
    mac = info.get("mac") or info.get("id")
    return Device(
        id=str(info.get("id", "")),
        name=str(info.get("name") or info.get("id") or hostname or "Shelly"),
        model=str(info.get("model", "")),
        app=str(info.get("app", "")),
        version=str(info.get("ver") or info.get("version") or ""),
        auth_enforced=bool(info.get("auth_enforced", info.get("auth", False))),
        mac=str(mac) if mac else None,
        hostname=hostname,
        addresses=addresses,
    )
