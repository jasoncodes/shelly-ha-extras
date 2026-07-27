from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlparse

from .models import AuthenticationError, ProtocolError


def _digest(value: str, algorithm: str) -> str:
    if algorithm.lower() in ("sha256", "sha-256"):
        return hashlib.sha256(value.encode()).hexdigest()
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def make_digest_auth(
    challenge: dict[str, Any],
    username: str,
    password: str,
    *,
    method: str = "RPC",
    uri: str = "/rpc",
) -> dict[str, str]:
    """Build both Shelly legacy and RFC7616-style authentication replies."""
    algorithm = str(challenge.get("algorithm", "MD5")).upper().replace("-", "")
    hash_algorithm = "SHA256" if algorithm == "SHA256" else "MD5"
    realm = str(challenge.get("realm", "shelly"))
    nonce = str(challenge.get("nonce", ""))
    cnonce = str(challenge.get("cnonce") or secrets.token_hex(8))
    ha1 = _digest(f"{username}:{realm}:{password}", hash_algorithm)
    qop = challenge.get("qop")
    nc = str(challenge.get("nc", "00000001"))
    if qop:
        ha2 = _digest(f"{method}:{uri}", hash_algorithm)
        response = _digest(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}", hash_algorithm)
    else:
        ha2 = _digest(f"{method}:{uri}", hash_algorithm)
        response = _digest(f"{ha1}:{nonce}:{ha2}", hash_algorithm)
    result = {
        "realm": realm,
        "username": username,
        "nonce": nonce,
        "cnonce": cnonce,
        "response": response,
    }
    if qop:
        result.update({"qop": str(qop), "nc": nc})
    return result


class ShellyRPC:
    """Correlated Shelly WebSocket RPC connection."""

    def __init__(
        self,
        target: str,
        *,
        username: str = "admin",
        password: str | None = None,
        connect: Callable[..., Awaitable[Any]] | None = None,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.target = target.removeprefix("ws://").removeprefix("wss://").rstrip("/")
        self.username = username
        self.password = password
        self.src = str(uuid.uuid4())
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._connect = connect
        self.on_notification = on_notification

    async def __aenter__(self) -> ShellyRPC:
        if self._connect:
            self._socket = await self._connect(self.uri)
        else:
            from websockets.asyncio.client import connect

            self._socket = await connect(self.uri, ping_interval=20, max_size=None)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._socket:
            await self._socket.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ProtocolError("WebSocket closed"))

    @property
    def uri(self) -> str:
        return f"ws://{self.target}/rpc"

    async def _read_loop(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                request_id = message.get("id")
                if request_id is None or request_id not in self._pending:
                    if self.on_notification and isinstance(message, dict):
                        value = self.on_notification(message)
                        if asyncio.iscoroutine(value):
                            await value
                    continue
                future = self._pending.pop(int(request_id))
                if not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        _retry_auth: bool = True,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request: dict[str, Any] = {"id": request_id, "src": self.src, "method": method}
        if params is not None:
            request["params"] = params
        try:
            await self._socket.send(json.dumps(request, separators=(",", ":")))
            reply = await asyncio.wait_for(future, timeout)
        except BaseException:
            self._pending.pop(request_id, None)
            raise
        if "error" in reply:
            error = reply["error"]
            if (
                _retry_auth
                and self.password
                and isinstance(error, dict)
                and (
                    error.get("code") in (401, 403)
                    or "nonce" in error
                    or error.get("auth_required")
                )
            ):
                auth = make_digest_auth(
                    error, self.username, self.password, method=method, uri="/rpc"
                )
                return await self.call(
                    method,
                    {**(params or {}), "auth": auth},
                    timeout=timeout,
                    _retry_auth=False,
                )
            if isinstance(error, dict) and error.get("code") in (401, 403):
                raise AuthenticationError("Shelly RPC authentication failed")
            raise ProtocolError(str(error))
        return cast(dict[str, Any], reply.get("result", {}))

    async def list_methods(self) -> list[str]:
        result = await self.call("Shelly.ListMethods")
        methods = result.get("methods", result) if isinstance(result, dict) else result
        return [str(method) for method in methods] if isinstance(methods, list) else []


ProgressCallback = Callable[[int, int], Awaitable[None] | None]


async def upload_firmware(
    rpc: ShellyRPC,
    firmware: bytes,
    *,
    progress: ProgressCallback | None = None,
    chunk_size: int = 2048,
) -> None:
    methods = await rpc.list_methods()
    required = {"OTA.Start", "OTA.Write", "OTA.Abort"}
    if not required.issubset(methods):
        raise ProtocolError("device does not expose the complete OTA RPC surface")
    size = len(firmware)
    offset = 0
    try:
        await rpc.call("OTA.Start", {"size": size})
        while offset < size:
            chunk = firmware[offset : offset + chunk_size]
            result = await rpc.call(
                "OTA.Write",
                {
                    "offset": offset,
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
            )
            acknowledged = result.get("offset")
            expected = offset + len(chunk)
            if (
                not isinstance(acknowledged, int)
                or isinstance(acknowledged, bool)
                or acknowledged <= offset
                or acknowledged > expected
                or acknowledged > size
            ):
                raise ProtocolError(f"OTA acknowledgement offset {acknowledged!r} is invalid")
            offset = acknowledged
            if progress:
                value = progress(offset, size)
                if asyncio.iscoroutine(value):
                    await value
        if offset != size:
            raise ProtocolError("OTA upload did not reach the exact firmware size")
    except BaseException:
        try:
            await rpc.call("OTA.Abort", timeout=5, _retry_auth=False)
        except Exception:
            pass
        raise


async def target_host(target: str) -> str:
    parsed = urlparse(target if "://" in target else f"http://{target}")
    if not parsed.hostname:
        raise ProtocolError("target must contain a hostname or address")
    return parsed.hostname
