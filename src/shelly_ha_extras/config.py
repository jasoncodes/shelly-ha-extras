from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "shelly-ha-extras"
    discovery_prefix: str = "homeassistant"
    client_id: str = "shelly-ha-extras"


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    interval_seconds: float = 300.0
    probe_seconds: float = 60.0
    stale_cleanup_days: float = 7.0


@dataclass(frozen=True, slots=True)
class PollingConfig:
    device_seconds: float = 300.0
    metadata_seconds: float = 21600.0
    verification_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class CacheConfig:
    directory: Path = Path("/data/cache")


@dataclass(frozen=True, slots=True)
class TLSConfig:
    insecure_diagnostic: bool = False
    allowed_hosts: tuple[str, ...] = ("updates.shelly.cloud", "fwcdn.shelly.cloud")
    ca_file: Path | None = Path("shelly_cloud.pem")


@dataclass(frozen=True, slots=True)
class AuthConfig:
    username: str = "admin"
    password: str | None = None
    passwords: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    json: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    data_directory: Path = Path("/data")
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    tls: TLSConfig = field(default_factory=TLSConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def password_for(self, device_id: str) -> str | None:
        return self.auth.passwords.get(device_id, self.auth.password)


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def _coerce_scalar(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is not None and type(None) in args:
        if value is None or value == "":
            return None
        annotation = next((arg for arg in args if arg is not type(None)), str)
    if annotation is bool and isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    if annotation is int and isinstance(value, str):
        return int(value)
    if annotation is float and isinstance(value, str):
        return float(value)
    return value


def _coerce(cls: type[Any], values: Mapping[str, Any]) -> Any:
    names = {f.name for f in fields(cls)}
    data = {k: v for k, v in values.items() if k in names}
    annotations = get_type_hints(cls)
    for key, value in list(data.items()):
        data[key] = _coerce_scalar(value, annotations[key])
    for key in ("directory", "ca_file"):
        if key in data and data[key] is not None:
            data[key] = Path(data[key])
    if "pins" in data:
        data["pins"] = tuple(data["pins"] if isinstance(data["pins"], list) else [data["pins"]])
    if "allowed_hosts" in data:
        allowed_hosts = data["allowed_hosts"]
        if isinstance(allowed_hosts, str):
            try:
                allowed_hosts = json.loads(allowed_hosts)
            except json.JSONDecodeError:
                allowed_hosts = [item.strip() for item in allowed_hosts.split(",") if item.strip()]
        data["allowed_hosts"] = tuple(allowed_hosts)
    if "passwords" in data and isinstance(data["passwords"], str):
        data["passwords"] = json.loads(data["passwords"])
    return cls(**data)


def load_config(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> Config:
    """Load defaults, TOML, SHELLY_HA_EXTRAS_* values, then CLI values."""
    environment = dict(os.environ if env is None else env)
    if path is None and environment.get("SHELLY_HA_EXTRAS_CONFIG_FILE"):
        path = Path(environment["SHELLY_HA_EXTRAS_CONFIG_FILE"])
    raw: dict[str, Any] = {}
    if path and path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)
    raw = _expand(raw, environment)
    cfg = Config()
    top = dict(raw)
    cache_directory_explicit = isinstance(top.get("cache"), dict) and "directory" in top["cache"]
    data_dir = top.pop("data_directory", None)
    if data_dir is not None:
        cfg = replace(cfg, data_directory=Path(data_dir))
    for section, cls in (
        ("mqtt", MQTTConfig),
        ("discovery", DiscoveryConfig),
        ("polling", PollingConfig),
        ("cache", CacheConfig),
        ("tls", TLSConfig),
        ("auth", AuthConfig),
        ("logging", LoggingConfig),
    ):
        if section in top:
            cfg = replace(cfg, **{section: _coerce(cls, top[section])})

    def set_path(root: str, key: str, value: str) -> None:
        nonlocal cfg
        section = getattr(cfg, root)
        cls = type(section)
        current = {f.name: getattr(section, f.name) for f in fields(cls)}
        current[key] = value
        cfg = replace(cfg, **{root: _coerce(cls, current)})

    for key, value in environment.items():
        if not key.startswith("SHELLY_HA_EXTRAS_"):
            continue
        tail = key[len("SHELLY_HA_EXTRAS_") :].lower()
        found = False
        for section, cls in (
            ("mqtt", MQTTConfig),
            ("discovery", DiscoveryConfig),
            ("polling", PollingConfig),
            ("cache", CacheConfig),
            ("tls", TLSConfig),
            ("auth", AuthConfig),
            ("logging", LoggingConfig),
        ):
            names = {f.name for f in fields(cls)}
            prefix = section + "_"
            if tail.startswith(prefix) and tail[len(prefix) :] in names:
                set_path(section, tail[len(prefix) :], _expand(value, environment))
                if section == "cache" and tail[len(prefix) :] == "directory":
                    cache_directory_explicit = True
                found = True
                break
        if not found and tail == "data_directory":
            cfg = replace(cfg, data_directory=Path(value))
    for key, value in (cli or {}).items():
        if value is None:
            continue
        if key == "data_directory":
            cfg = replace(cfg, data_directory=Path(value))
        elif "_" in key:
            section, field_name = key.split("_", 1)
            if hasattr(cfg, section) and hasattr(getattr(cfg, section), field_name):
                set_path(section, field_name, value)
                if section == "cache" and field_name == "directory":
                    cache_directory_explicit = True
    if not cache_directory_explicit:
        cfg = replace(cfg, cache=replace(cfg.cache, directory=cfg.data_directory / "cache"))
    return cfg
