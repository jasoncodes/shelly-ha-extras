from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import Any

import typer

from .cache import cache_firmware, cache_path, validate_zip
from .config import Config, load_config
from .device import get_device_info, install
from .discovery import connection_target, discover
from .http import ShellyHTTP
from .metadata import fetch_latest_stable, fetch_update, require_official, version_key
from .models import ShellyError
from .rpc import ShellyRPC
from .schedule import list_schedules, simple_time, update_schedule

app = typer.Typer(no_args_is_help=True, help="Shelly Home Assistant Extras")
devices_app = typer.Typer(no_args_is_help=True, help="Discover and inspect devices")
firmware_app = typer.Typer(no_args_is_help=True, help="Check and install firmware")
schedules_app = typer.Typer(no_args_is_help=True, help="Inspect and update schedules")
app.add_typer(devices_app, name="devices")
app.add_typer(firmware_app, name="firmware")
app.add_typer(schedules_app, name="schedules")


def _config(config_file: Path | None, data_directory: Path | None = None) -> Config:
    return load_config(config_file, cli={"data_directory": data_directory})


def _configure_logging(config: Config) -> None:
    # httpx emits one INFO line for every request. Keep those details behind
    # our explicit HTTP debug messages so normal bridge output stays useful.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, default=str, sort_keys=True))
    elif isinstance(value, list):
        for item in value:
            typer.echo("  ".join(str(v) for v in item.values()))
    else:
        for key, item in value.items():
            typer.echo(f"{key}: {item}")


def _print_discovery(rows: list[dict[str, Any]], check_updates: bool) -> None:
    columns = ["id", "hostname", "addresses", "name", "model", "installed_version"]
    if check_updates:
        columns.extend(["latest_version", "update_available"])
    columns.append("error")
    values: list[list[str]] = []
    for row in rows:
        values.append(
            [
                ",".join(str(address) for address in row.get(column, ()))
                if column == "addresses"
                else "" if row.get(column) is None else str(row.get(column, ""))
                for column in columns
            ]
        )
    widths = [
        max(len(column), *(len(rendered[index]) for rendered in values))
        for index, column in enumerate(columns)
    ]
    typer.echo("  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)))
    for rendered in values:
        typer.echo(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(rendered))
        )


@devices_app.command("discover")
def discover_devices(
    timeout: float = typer.Option(3.0),
    json_output: bool = typer.Option(False, "--json"),
    check_updates: bool = typer.Option(False, "--check-updates", "--updates"),
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """Discover Shelly devices, optionally checking stable firmware status."""
    try:
        devices = asyncio.run(discover(timeout))
    except ShellyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _config(config_file)
    http = ShellyHTTP(cfg.tls) if check_updates else None

    async def statuses() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for discovered in devices:
            row: dict[str, Any] = {
                "hostname": discovered.hostname,
                "addresses": discovered.addresses,
                "port": discovered.port,
                "properties": discovered.properties,
            }
            try:
                device = await get_device_info(
                    connection_target(discovered),
                    username=cfg.auth.username,
                    password=cfg.password_for(discovered.hostname),
                )
                row.update(
                    {
                        "id": device.id,
                        "name": device.name,
                        "model": device.model,
                        "app": device.app,
                        "installed_version": device.version,
                    }
                )
                if http is not None:
                    latest = fetch_latest_stable(http, device.app)
                    row.update(
                        {
                            "latest_version": latest.version if latest else None,
                            "update_available": bool(
                                latest
                                and version_key(latest.version) > version_key(device.version)
                            ),
                        }
                    )
            except Exception as exc:
                row["error"] = str(exc)
            result.append(row)
        return result

    output = asyncio.run(statuses())
    if json_output:
        _print(output, True)
    else:
        _print_discovery(output, check_updates)


@devices_app.command("info")
def device_info(
    target: str,
    json_output: bool = typer.Option(False, "--json"),
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """Read device information for TARGET."""
    cfg = _config(config_file)
    try:
        device = asyncio.run(
            get_device_info(target, username=cfg.auth.username, password=cfg.password_for(target))
        )
        _print(
            {
                "id": device.id,
                "name": device.name,
                "model": device.model,
                "app": device.app,
                "version": device.version,
                "auth_enforced": device.auth_enforced,
                "hostname": device.hostname,
                "addresses": device.addresses,
            },
            json_output,
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@firmware_app.command("check")
def firmware_check(
    target: str,
    json_output: bool = typer.Option(False, "--json"),
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """Compare TARGET with the latest official stable firmware."""
    cfg = _config(config_file)
    try:
        device = asyncio.run(
            get_device_info(target, username=cfg.auth.username, password=cfg.password_for(target))
        )
        latest = fetch_latest_stable(ShellyHTTP(cfg.tls), device.app)
        _print(
            {
                "id": device.id,
                "name": device.name,
                "model": device.model,
                "app": device.app,
                "installed_version": device.version,
                "latest_version": latest.version if latest else None,
                "update_available": bool(
                    latest and version_key(latest.version) > version_key(device.version)
                ),
                "hostname": device.hostname,
                "addresses": device.addresses,
            },
            json_output,
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@firmware_app.command("install")
def firmware_install(
    target: str,
    yes: bool = typer.Option(False, "--yes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config_file: Path | None = typer.Option(None, "--config"),
    data_directory: Path | None = typer.Option(None, "--data-directory"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Install the official stable firmware for TARGET."""
    cfg = _config(config_file, data_directory)

    async def run() -> dict[str, Any]:
        device = await get_device_info(
            target, username=cfg.auth.username, password=cfg.password_for(target)
        )
        http = ShellyHTTP(cfg.tls)
        info = fetch_update(http, device.app, device.version)
        if info is None:
            return {
                "device": device.id,
                "installed_version": device.version,
                "latest_version": device.version,
                "update_available": False,
                "updated": False,
                "dry_run": dry_run,
            }
        require_official(info)
        if dry_run:
            return {
                "device": device.id,
                "installed_version": device.version,
                "latest_version": info.version,
                "update_available": True,
                "updated": False,
                "dry_run": True,
            }
        if not yes and not typer.confirm(f"Install Shelly {info.version} on {device.name}?"):
            raise typer.Abort()
        path = cache_path(cfg.cache.directory, info)
        if path.exists():
            validate_zip(path, expected_size=info.size, expected_sha256=info.sha256)
        else:
            response = http.stream(info.url)
            try:
                path = cache_firmware(cfg.cache.directory, info, response)
            finally:
                response.close()
        data = path.read_bytes()
        acknowledged = {"bytes": 0, "size": len(data)}

        async def progress(done: int, size: int) -> None:
            acknowledged.update(bytes=done, size=size)
            if not json_output:
                typer.echo(f"{done}/{size} bytes ({done / size:.1%})")

        result = await install(
            target,
            data,
            device.id,
            username=cfg.auth.username,
            password=cfg.password_for(device.id),
            progress=progress,
            verification_seconds=cfg.polling.verification_seconds,
            poll_seconds=5,
            expected_version=info.version,
        )
        return {
            "device": result.id,
            "version": result.version,
            "updated": True,
            "bytes": acknowledged["bytes"],
            "size": acknowledged["size"],
        }

    try:
        _print(asyncio.run(run()), json_output)
    except ShellyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


async def _schedule_rpc(target: str, cfg: Config) -> ShellyRPC:
    return ShellyRPC(target, username=cfg.auth.username, password=cfg.password_for(target))


@schedules_app.command("list")
def schedules_list(
    target: str,
    json_output: bool = typer.Option(False, "--json"),
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """List the existing Shelly schedule jobs for TARGET."""
    cfg = _config(config_file)

    async def run() -> dict[str, Any]:
        async with await _schedule_rpc(target, cfg) as rpc:
            return (await list_schedules(rpc)).as_dict()

    try:
        _print(asyncio.run(run()), json_output)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@schedules_app.command("update")
def schedules_update(
    target: str,
    job_id: int,
    enable: bool = typer.Option(False, "--enable"),
    disable: bool = typer.Option(False, "--disable"),
    timespec: str | None = typer.Option(None, "--timespec"),
    json_output: bool = typer.Option(False, "--json"),
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """Update an existing schedule job's enabled state or cron TIMESPEC."""
    if enable and disable:
        raise typer.BadParameter("--enable and --disable are mutually exclusive")
    if not (enable or disable or timespec):
        raise typer.BadParameter("provide --enable, --disable, or --timespec")
    cfg = _config(config_file)

    async def run() -> dict[str, Any]:
        async with await _schedule_rpc(target, cfg) as rpc:
            return await update_schedule(
                rpc,
                job_id,
                enable=True if enable else False if disable else None,
                timespec=timespec,
            )

    try:
        _print(asyncio.run(run()), json_output)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@schedules_app.command("eval")
def schedules_eval(
    target: str,
    timespec: str,
    config_file: Path | None = typer.Option(None, "--config"),
) -> None:
    """Evaluate whether TIMESPEC can use the Home Assistant time entity."""
    value = simple_time(timespec)
    rendered = None if value is None else f"{value[0]:02d}:{value[1]:02d}:{value[2]:02d}"
    _print({"timespec": timespec, "simple": value is not None, "time": rendered}, False)


@app.command()
def healthcheck(config_file: Path | None = typer.Option(None, "--config")) -> None:
    """Check that the configured data directory is writable."""
    cfg = _config(config_file)
    try:
        cfg.data_directory.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_directory / ".healthcheck"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        typer.echo(f"unhealthy: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("ok")


@app.command()
def run(config_file: Path | None = typer.Option(None, "--config")) -> None:
    """Run MQTT/Home Assistant discovery and firmware command handling."""
    from .mqtt import serve_forever

    cfg = _config(config_file)
    _configure_logging(cfg)

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        serve_forever(cfg, discover)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutdown requested")


if __name__ == "__main__":
    app()
