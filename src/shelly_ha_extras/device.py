from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from .models import Device, VersionTimeout, device_from_info
from .rpc import ShellyRPC, upload_firmware


def device_url(target: str, path: str) -> str:
    target = target.removeprefix("http://").removeprefix("https://").rstrip("/")
    return f"http://{target}{path}"


async def get_device_info(
    target: str,
    *,
    timeout: float = 10,
    client: httpx.AsyncClient | None = None,
    username: str | None = None,
    password: str | None = None,
) -> Device:
    own = client is None
    actual = client or httpx.AsyncClient(timeout=timeout)
    try:
        auth = (username, password) if username and password else None
        response = await actual.get(device_url(target, "/rpc/Shelly.GetDeviceInfo"), auth=auth)
        response.raise_for_status()
        payload = response.json()
        return device_from_info(payload.get("result", payload), hostname=target)
    finally:
        if own:
            await actual.aclose()


class InstallationCoordinator:
    def __init__(self) -> None:
        self._global = asyncio.Lock()
        self._devices: dict[str, asyncio.Lock] = {}

    def lock_for(self, device_id: str) -> asyncio.Lock:
        return self._devices.setdefault(device_id, asyncio.Lock())

    def locks(self, device_id: str) -> tuple[asyncio.Lock, asyncio.Lock]:
        return self._global, self.lock_for(device_id)


async def install(
    target: str,
    firmware: bytes,
    device_id: str,
    *,
    username: str = "admin",
    password: str | None = None,
    coordinator: InstallationCoordinator | None = None,
    progress: Callable[[int, int], Awaitable[None] | None] | None = None,
    poll_seconds: float = 5,
    verification_seconds: float = 300,
    expected_version: str | None = None,
    rpc_factory: Callable[..., ShellyRPC] = ShellyRPC,
) -> Device:
    coordinator = coordinator or InstallationCoordinator()
    global_lock, device_lock = coordinator.locks(device_id)
    async with global_lock, device_lock:
        rpc = rpc_factory(target, username=username, password=password)
        try:
            async with rpc:
                await upload_firmware(rpc, firmware, progress=progress)
        except Exception:
            # The device can close the socket immediately after final acknowledgement;
            # caller-side verification below is only attempted after a successful upload.
            raise
        if expected_version:
            deadline = time.monotonic() + verification_seconds
            while time.monotonic() < deadline:
                try:
                    current = await get_device_info(target)
                    if current.version == expected_version:
                        return current
                except (OSError, httpx.HTTPError):
                    pass
                await asyncio.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
            raise VersionTimeout(f"device did not report firmware {expected_version} in time")
        return await get_device_info(target)
