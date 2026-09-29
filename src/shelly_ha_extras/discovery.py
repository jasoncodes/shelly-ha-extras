from __future__ import annotations

import asyncio
import logging
import socket
import threading
from dataclasses import dataclass
from typing import Any, cast

from .models import DiscoveryError

SHELLY_SERVICE_TYPE = "_shelly._tcp.local."
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    hostname: str
    addresses: tuple[str, ...]
    port: int
    properties: dict[str, str]


def connection_target(device: DiscoveredDevice) -> str:
    """Return the directly reachable advertised address for a device."""
    if device.addresses:
        address = device.addresses[0]
        return f"[{address}]" if ":" in address else address
    return device.hostname


def _decode_txt(value: object) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def resolve_local(hostname: str) -> tuple[str, ...]:
    """Use the operating system resolver first, preserving the .local name."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    return tuple(dict.fromkeys(cast(str, info[4][0]) for info in infos))


class DiscoveryWatcher:
    """Continuous Shelly mDNS listener for the long-running MQTT bridge."""

    def __init__(self) -> None:
        self._found: dict[str, DiscoveredDevice] = {}
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._zc: Any = None
        self._browser: Any = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        except ImportError as exc:
            raise DiscoveryError("zeroconf is required for continuous discovery") from exc
        owner = self

        class Listener(ServiceListener):
            def add_service(self, zc: Any, service_type: str, name: str) -> None:
                service = zc.get_service_info(service_type, name)
                if service:
                    device = DiscoveredDevice(
                        service.server.rstrip("."),
                        tuple(
                            str(address)
                            for address in dict.fromkeys(service.parsed_addresses())
                        ),
                        service.port,
                        {
                            _decode_txt(k): _decode_txt(v)
                            for k, v in service.properties.items()
                        },
                    )
                    with owner._lock:
                        owner._found[name] = device
                    owner._changed.set()

            def update_service(self, zc: Any, service_type: str, name: str) -> None:
                self.add_service(zc, service_type, name)

            def remove_service(self, zc: Any, service_type: str, name: str) -> None:
                with owner._lock:
                    device = owner._found.pop(name, None)
                if device is not None:
                    LOGGER.info("mDNS service removed %s (%s)", name, device.hostname)
                owner._changed.set()

        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, SHELLY_SERVICE_TYPE, Listener())
        except OSError as exc:
            raise DiscoveryError(f"could not open the mDNS socket: {exc}") from exc

    def wait(self, timeout: float) -> bool:
        changed = self._changed.wait(timeout)
        self._changed.clear()
        return changed

    def snapshot(self) -> list[DiscoveredDevice]:
        with self._lock:
            return sorted(self._found.values(), key=lambda d: d.hostname.lower())

    def close(self) -> None:
        if self._browser:
            self._browser.cancel()
        if self._zc:
            self._zc.close()


async def discover(timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Discover Shelly devices via the Shelly-specific DNS-SD service."""
    try:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    except ImportError as exc:
        raise DiscoveryError("zeroconf is required for scan fallback") from exc

    found: dict[str, DiscoveredDevice] = {}
    names: dict[str, str] = {}
    pending: set[asyncio.Task[None]] = set()

    async def resolve(service_type: str, name: str) -> None:
        service = await zc.async_get_service_info(service_type, name)
        if service and service.server is not None and service.port is not None:
            hostname = service.server.rstrip(".")
            addresses = tuple(str(address) for address in dict.fromkeys(service.parsed_addresses()))
            properties = {_decode_txt(k): _decode_txt(v) for k, v in service.properties.items()}
            found[hostname] = DiscoveredDevice(hostname, addresses, service.port, properties)
            names[name] = hostname

    def on_service(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            hostname = names.pop(name, None)
            if hostname is not None:
                found.pop(hostname, None)
            return
        task = asyncio.create_task(resolve(service_type, name))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        zc = AsyncZeroconf()
    except OSError as exc:
        raise DiscoveryError(f"could not open the mDNS socket: {exc}") from exc
    try:
        browser = AsyncServiceBrowser(zc.zeroconf, SHELLY_SERVICE_TYPE, handlers=[on_service])
    except BaseException:
        await zc.async_close()
        raise
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await zc.async_close()
    return sorted(found.values(), key=lambda d: d.hostname.lower())
