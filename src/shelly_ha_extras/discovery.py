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
    service_name: str = ""


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
        self._scan_misses: dict[str, int] = {}
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
                        name,
                    )
                    with owner._lock:
                        owner._found[name] = device
                        owner._scan_misses.pop(name, None)
                    owner._changed.set()

            def update_service(self, zc: Any, service_type: str, name: str) -> None:
                self.add_service(zc, service_type, name)

            def remove_service(self, zc: Any, service_type: str, name: str) -> None:
                with owner._lock:
                    device = owner._found.pop(name, None)
                    owner._scan_misses.pop(name, None)
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

    def reconcile_scan(self, scanned: list[DiscoveredDevice]) -> None:
        """Retire services absent from two successful independent scans."""
        present = {device.service_name for device in scanned if device.service_name}
        removed: list[tuple[str, str]] = []
        with self._lock:
            for name, device in list(self._found.items()):
                if name in present:
                    self._scan_misses.pop(name, None)
                    continue
                misses = self._scan_misses.get(name, 0) + 1
                if misses < 2:
                    self._scan_misses[name] = misses
                    continue
                del self._found[name]
                self._scan_misses.pop(name, None)
                removed.append((name, device.hostname))
        for name, hostname in removed:
            LOGGER.info("mDNS service absent from repeated scans %s (%s)", name, hostname)
        if removed:
            self._changed.set()

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
    pending: set[asyncio.Task[None]] = set()
    resolving: dict[str, asyncio.Task[None]] = {}

    async def resolve(service_type: str, name: str) -> None:
        service = await zc.async_get_service_info(service_type, name)
        if service and service.server is not None and service.port is not None:
            hostname = service.server.rstrip(".")
            addresses = tuple(str(address) for address in dict.fromkeys(service.parsed_addresses()))
            properties = {_decode_txt(k): _decode_txt(v) for k, v in service.properties.items()}
            found[name] = DiscoveredDevice(hostname, addresses, service.port, properties, name)

    def on_service(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        from zeroconf import ServiceStateChange

        previous = resolving.pop(name, None)
        if previous is not None:
            previous.cancel()
        if state_change is ServiceStateChange.Removed:
            found.pop(name, None)
            return
        task = asyncio.create_task(resolve(service_type, name))
        pending.add(task)
        resolving[name] = task

        def finished(done: asyncio.Task[None]) -> None:
            pending.discard(done)
            if resolving.get(name) is done:
                resolving.pop(name, None)

        task.add_done_callback(finished)

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
