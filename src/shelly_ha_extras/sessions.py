from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

from .models import Device
from .rpc import ShellyRPC
from .schedule import ScheduleSnapshot, list_schedules, update_schedule

LOGGER = logging.getLogger(__name__)


class ShellySession:
    """A reconnecting, long-lived RPC session for one Shelly device."""

    def __init__(
        self,
        device: Device,
        target: str,
        *,
        username: str,
        password: str | None,
        on_snapshot: Callable[[Device, ScheduleSnapshot], None],
        on_connection_change: Callable[[Device, bool], None],
        refresh_seconds: float = 300.0,
    ) -> None:
        self.device = device
        self.target = target
        self.username = username
        self.password = password
        self.on_snapshot = on_snapshot
        self.on_connection_change = on_connection_change
        self.refresh_seconds = refresh_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rpc: ShellyRPC | None = None
        self._stop_async: asyncio.Event | None = None
        self._retry_async: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._connected: bool | None = None

    def _set_connected(self, connected: bool) -> None:
        if self._connected != connected:
            self._connected = connected
            self.on_connection_change(self.device, connected)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop:
            if self._stop_async:
                loop.call_soon_threadsafe(self._stop_async.set)
            if self._retry_async:
                loop.call_soon_threadsafe(self._retry_async.set)
        self._thread.join(timeout=5)

    def request_reconnect(self) -> None:
        loop = self._loop
        retry = self._retry_async
        if loop and retry and not self._ready.is_set():
            loop.call_soon_threadsafe(retry.set)

    def update(
        self,
        job_id: int,
        *,
        enable: bool | None = None,
        timespec: str | None = None,
    ) -> None:
        LOGGER.debug("queueing schedule update for %s job %s", self.device.id, job_id)
        if not self._ready.wait(30) or not self._loop:
            raise RuntimeError("Shelly RPC session is not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._update(job_id, enable=enable, timespec=timespec), self._loop
        )
        future.result(timeout=30)
        LOGGER.debug("schedule update completed for %s job %s", self.device.id, job_id)

    async def _update(self, job_id: int, *, enable: bool | None, timespec: str | None) -> None:
        if not self._rpc:
            raise RuntimeError("Shelly RPC session is not connected")
        await update_schedule(self._rpc, job_id, enable=enable, timespec=timespec)
        self.on_snapshot(self.device, await list_schedules(self._rpc))

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        self._retry_async = asyncio.Event()
        backoff = 1.0
        while not self._stop.is_set():
            retry_delay = 0.0
            try:
                async with ShellyRPC(
                    self.target,
                    username=self.username,
                    password=self.password,
                    on_notification=self._notification,
                ) as rpc:
                    self._rpc = rpc
                    self._ready.set()
                    self._set_connected(True)
                    self._retry_async.clear()
                    backoff = 1.0
                    LOGGER.info("Shelly RPC session connected for %s", self.device.id)
                    self.on_snapshot(self.device, await list_schedules(rpc))
                    while not self._stop.is_set():
                        try:
                            await asyncio.wait_for(
                                self._stop_async.wait(), timeout=self.refresh_seconds
                            )
                        except TimeoutError:
                            self.on_snapshot(self.device, await list_schedules(rpc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Shelly RPC session failed for %s: %s", self.device.id, exc)
                retry_delay = backoff
                backoff = min(backoff * 2, 60.0)
            finally:
                self._rpc = None
                self._ready.clear()
                self._set_connected(False)
            if retry_delay and not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._retry_async.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                self._retry_async.clear()

    def _notification(self, message: dict[str, Any]) -> None:
        if "schedule_rev" in repr(message):
            loop = self._loop
            if loop and self._rpc:
                asyncio.create_task(self._refresh())

    async def _refresh(self) -> None:
        if self._rpc:
            self.on_snapshot(self.device, await list_schedules(self._rpc))


class SessionManager:
    def __init__(
        self,
        *,
        username: str,
        password_for: Callable[[str], str | None],
        on_snapshot: Callable[[Device, ScheduleSnapshot], None],
        on_connection_change: Callable[[Device, bool], None],
    ) -> None:
        self.username = username
        self.password_for = password_for
        self.on_snapshot = on_snapshot
        self.on_connection_change = on_connection_change
        self.sessions: dict[str, ShellySession] = {}

    def ensure(
        self,
        device: Device,
        target: str,
        refresh_seconds: float = 300.0,
        *,
        reconnect: bool = False,
    ) -> ShellySession:
        session = self.sessions.get(device.id)
        if session and session.target == target:
            if reconnect:
                session.request_reconnect()
            return session
        if session:
            session.stop()
        session = ShellySession(
            device,
            target,
            username=self.username,
            password=self.password_for(device.id),
            on_snapshot=self.on_snapshot,
            on_connection_change=self.on_connection_change,
            refresh_seconds=refresh_seconds,
        )
        self.sessions[device.id] = session
        session.start()
        return session

    def stop(self) -> None:
        for session in self.sessions.values():
            session.stop()
        self.sessions.clear()
