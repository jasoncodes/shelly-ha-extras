from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .rpc import ShellyRPC


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    jobs: list[dict[str, Any]]
    rev: int | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"jobs": self.jobs}
        if self.rev is not None:
            value["rev"] = self.rev
        return value


async def list_schedules(rpc: ShellyRPC) -> ScheduleSnapshot:
    result = await rpc.call("schedule.list")
    jobs = result.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("schedule.list returned an invalid jobs list")
    rev = result.get("rev")
    return ScheduleSnapshot(jobs=[job for job in jobs if isinstance(job, dict)], rev=rev)


async def update_schedule(
    rpc: ShellyRPC,
    job_id: int,
    *,
    enable: bool | None = None,
    timespec: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"id": job_id}
    if enable is not None:
        params["enable"] = enable
    if timespec is not None:
        params["timespec"] = timespec
    if len(params) == 1:
        raise ValueError("schedule update needs --enable, --disable, or --timespec")
    return await rpc.call("schedule.update", params)


_NUMBER = re.compile(r"^\d+$")


def simple_time(timespec: str) -> tuple[int, int, int] | None:
    """Return hour/minute/seconds for a single daily cron time.

    Shelly uses six or seven cron fields: seconds, minutes, hours, day,
    month, weekday, and optionally year. A time entity is only safe when the
    first three fields are single numeric values; all remaining fields must
    select every day.
    """
    fields = timespec.split()
    if len(fields) not in (6, 7):
        return None
    if not all(_NUMBER.fullmatch(field) for field in fields[:3]):
        return None
    if fields[3:] != ["*", "*", "0,1,2,3,4,5,6"] and fields[3:] != ["*", "*", "0,1,2,3,4,5,6", "*"]:
        return None
    seconds, minute, hour = (int(field) for field in fields[:3])
    if seconds != 0 or minute > 59 or hour > 23:
        return None
    return hour, minute, seconds


def time_string(timespec: str) -> str | None:
    value = simple_time(timespec)
    return None if value is None else f"{value[0]:02d}:{value[1]:02d}:{value[2]:02d}"


def replace_time(timespec: str, value: str) -> str:
    """Replace the seconds, minutes, and hours in a Shelly cron expression."""
    fields = timespec.split()
    if len(fields) not in (6, 7):
        raise ValueError("schedule timespec must have six or seven fields")
    parts = value.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("schedule time must use HH:MM:SS format")
    hour, minute, seconds = (int(part) for part in parts)
    if seconds > 59 or minute > 59 or hour > 23:
        raise ValueError("schedule time is outside the valid range")
    return " ".join([str(seconds), str(minute), str(hour), *fields[3:]])
