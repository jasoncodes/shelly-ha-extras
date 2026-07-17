import asyncio
import base64
import hashlib
import json

import pytest

from shelly_ha_extras.models import ProtocolError
from shelly_ha_extras.rpc import ShellyRPC, make_digest_auth, upload_firmware


class FakeRPC:
    def __init__(self, *, bad=False, fail_at=None):
        self.bad = bad
        self.fail_at = fail_at
        self.calls = []

    async def list_methods(self):
        return ["OTA.Start", "OTA.Write", "OTA.Abort"]

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        if method == "OTA.Write":
            offset = params["offset"]
            chunk_len = len(base64.b64decode(params["data"]))
            if self.fail_at == offset:
                raise ConnectionError("reboot")
            return {"offset": offset if self.bad else offset + chunk_len}
        return {}


@pytest.mark.asyncio
async def test_ota_uses_2048_byte_chunks_and_ack_offsets():
    rpc = FakeRPC()
    progress = []
    data = bytes(range(256)) * 9
    await upload_firmware(rpc, data, progress=lambda done, size: progress.append((done, size)))
    writes = [params for method, params in rpc.calls if method == "OTA.Write"]
    assert [len(base64.b64decode(p["data"])) for p in writes] == [2048, 256]
    assert progress[-1] == (len(data), len(data))


@pytest.mark.asyncio
async def test_ota_malformed_ack_aborts():
    rpc = FakeRPC(bad=True)
    with pytest.raises(ProtocolError):
        await upload_firmware(rpc, b"firmware")
    assert [method for method, _ in rpc.calls][-1] == "OTA.Abort"


@pytest.mark.asyncio
async def test_ota_disconnect_best_effort_abort():
    rpc = FakeRPC(fail_at=0)
    with pytest.raises(ConnectionError):
        await upload_firmware(rpc, b"firmware")
    assert [method for method, _ in rpc.calls][-1] == "OTA.Abort"


class PartialAckRPC(FakeRPC):
    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        if method == "OTA.Write":
            offset = params["offset"]
            chunk_len = len(base64.b64decode(params["data"]))
            return {"offset": offset + min(1570, chunk_len)}
        return {}


@pytest.mark.asyncio
async def test_ota_resumes_from_a_forward_partial_ack():
    rpc = PartialAckRPC()
    data = bytes(range(256)) * 9
    await upload_firmware(rpc, data)
    writes = [params for method, params in rpc.calls if method == "OTA.Write"]
    assert writes[0]["offset"] == 0
    assert writes[1]["offset"] == 1570


def test_legacy_md5_digest():
    challenge = {"realm": "shelly", "nonce": "abc", "algorithm": "MD5"}
    result = make_digest_auth(challenge, "admin", "pw")
    ha1 = hashlib.md5(b"admin:shelly:pw").hexdigest()
    ha2 = hashlib.md5(b"RPC:/rpc").hexdigest()
    assert result["response"] == hashlib.md5(f"{ha1}:abc:{ha2}".encode()).hexdigest()


class CorrelatingSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        await self.messages.put(json.dumps({"src": "device", "method": "Notify"}))
        await self.messages.put(json.dumps({"id": request["id"], "result": {"ok": True}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.messages.get()

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_rpc_ignores_interleaved_notifications_and_correlates_ids():
    socket = CorrelatingSocket()

    async def connect(_uri):
        return socket

    async with ShellyRPC("device.local", connect=connect) as rpc:
        assert await rpc.call("One") == {"ok": True}
        assert await rpc.call("Two") == {"ok": True}
    assert [message["id"] for message in socket.sent] == [1, 2]
    assert socket.sent[0]["src"] == socket.sent[1]["src"]
