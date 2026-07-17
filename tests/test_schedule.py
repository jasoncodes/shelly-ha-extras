import pytest

from shelly_ha_extras.schedule import (
    ScheduleSnapshot,
    replace_time,
    simple_time,
    time_string,
    update_schedule,
)


def test_simple_daily_timespec() -> None:
    assert simple_time("0 30 14 * * 0,1,2,3,4,5,6") == (14, 30, 0)
    assert time_string("0 30 14 * * 0,1,2,3,4,5,6") == "14:30:00"


@pytest.mark.parametrize(
    "timespec",
    [
        "1 30 14 * * 0,1,2,3,4,5,6",
        "0 */15 14 * * 0,1,2,3,4,5,6",
        "0 30 14 * * 1-5",
        "0 30 14 1 * 0,1,2,3,4,5,6",
    ],
)
def test_complex_timespec_is_not_time_entity_eligible(timespec: str) -> None:
    assert simple_time(timespec) is None


def test_snapshot_omits_unknown_revision() -> None:
    assert ScheduleSnapshot([{"id": 1}]).as_dict() == {"jobs": [{"id": 1}]}


def test_replace_time_preserves_cron_days() -> None:
    assert replace_time("0 30 9 * * 0,1,2,3,4,5,6", "14:05:00") == (
        "0 5 14 * * 0,1,2,3,4,5,6"
    )


@pytest.mark.asyncio
async def test_update_schedule_uses_shelly_params() -> None:
    class FakeRPC:
        async def call(self, method, params):
            assert method == "schedule.update"
            assert params == {"id": 2, "enable": True, "timespec": "0 30 9 * * 0,1,2,3,4,5,6"}
            return {"rev": 6}

    result = await update_schedule(
        FakeRPC(), 2, enable=True, timespec="0 30 9 * * 0,1,2,3,4,5,6"
    )
    assert result == {"rev": 6}
