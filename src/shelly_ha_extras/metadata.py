from __future__ import annotations

from typing import Any

from .http import ShellyHTTP
from .models import IntegrityError, UpdateInfo


def _version(value: Any) -> str:
    return str(value or "").strip()


def version_key(value: str) -> tuple[tuple[int, ...], str]:
    numbers: list[int] = []
    suffix = ""
    for part in value.split("."):
        digits = "".join(c for c in part if c.isdigit())
        if digits:
            numbers.append(int(digits))
        if not part.isdigit():
            suffix = part
    return tuple(numbers), suffix


def stable_update(payload: dict[str, Any], app: str) -> UpdateInfo | None:
    """Accept the CDN's common list and mapping forms, rejecting beta/downgrades."""
    candidates: list[dict[str, Any]] = []
    if isinstance(payload.get("updates"), list):
        candidates.extend(x for x in payload["updates"] if isinstance(x, dict))
    elif isinstance(payload.get("versions"), list):
        candidates.extend(x for x in payload["versions"] if isinstance(x, dict))
    elif isinstance(payload.get("stable"), dict):
        candidates.append(payload["stable"])
    else:
        candidates.append(payload)
    stable: list[dict[str, Any]] = []
    for item in candidates:
        channel = str(item.get("channel", item.get("branch", "stable"))).lower()
        if channel != "stable" or item.get("beta") is True or item.get("is_beta") is True:
            continue
        url = item.get("url") or item.get("download_url") or item.get("file")
        version = item.get("version") or item.get("ver")
        if isinstance(url, str) and isinstance(version, str):
            stable.append(item)
    if not stable:
        return None
    item = max(
        stable,
        key=lambda candidate: version_key(
            _version(candidate.get("version") or candidate.get("ver"))
        ),
    )
    size = item.get("size") or item.get("content_length")
    return UpdateInfo(
        app=app,
        version=_version(item.get("version") or item.get("ver")),
        url=str(item.get("url") or item.get("download_url") or item["file"]),
        sha256=item.get("sha256") or item.get("sha256sum") or item.get("checksum"),
        size=int(size) if size is not None else None,
    )


def fetch_update(http: ShellyHTTP, app: str, current: str) -> UpdateInfo | None:
    info = fetch_latest_stable(http, app)
    if info is None or version_key(info.version) <= version_key(current):
        return None
    return info


def fetch_latest_stable(http: ShellyHTTP, app: str) -> UpdateInfo | None:
    """Read stable metadata without comparing it to an installed version."""
    payload = http.get_json(f"https://updates.shelly.cloud/update/{app}")
    info = stable_update(payload, app)
    if info is not None:
        require_official(info)
    return info


def require_official(info: UpdateInfo) -> None:
    if not (
        info.url.startswith("https://updates.shelly.cloud/")
        or info.url.startswith("https://fwcdn.shelly.cloud/")
    ):
        raise IntegrityError("firmware URL is not the official Shelly CDN")
    if info.channel != "stable":
        raise IntegrityError("only stable firmware is supported")
