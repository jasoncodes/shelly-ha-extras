from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

import httpx

from .config import TLSConfig
from .tls import ssl_context, validate_certificate, validate_url

LOGGER = logging.getLogger(__name__)


class ShellyStream:
    def __init__(self, client: httpx.Client, response: httpx.Response) -> None:
        self._client = client
        self._response = response

    @property
    def headers(self) -> httpx.Headers:
        return self._response.headers

    @property
    def status_code(self) -> int:
        return self._response.status_code

    def iter_bytes(self, chunk_size: int | None = None) -> Iterator[bytes]:
        if chunk_size is None:
            yield from self._response.iter_bytes()
        else:
            yield from self._response.iter_bytes(chunk_size=chunk_size)

    def close(self) -> None:
        self._response.close()
        self._client.close()


class ShellyHTTP:
    def __init__(self, tls: TLSConfig, *, timeout: float = 30.0) -> None:
        self.tls = tls
        self.timeout = timeout

    def client(self) -> httpx.Client:
        return httpx.Client(
            verify=ssl_context(self.tls), timeout=self.timeout, follow_redirects=False
        )

    def get_json(self, url: str) -> dict[str, Any]:
        LOGGER.debug("HTTP GET %s", url)
        host = validate_url(url, self.tls)
        validate_certificate(host, self.tls)
        with self.client() as client:
            response = client.get(url)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def stream(self, url: str) -> ShellyStream:
        LOGGER.debug("HTTP streaming GET %s", url)
        host = validate_url(url, self.tls)
        validate_certificate(host, self.tls)
        client = self.client()
        request = client.build_request("GET", url)
        return ShellyStream(client, client.send(request, stream=True))
