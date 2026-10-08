"""Buffer and push-stat persistence across restarts (#18, #19)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry import coordinator as coord_mod
from custom_components.nem_flex_telemetry.const import (
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.nem_flex_telemetry.coordinator import (
    NemFlexTelemetryCoordinator,
)
from custom_components.nem_flex_telemetry.github_client import (
    NemFlexGitHubClient,
    dedupe_new_lines,
)

DATA = {
    CONF_HOUSEHOLD_ID: "00000000-0000-4000-8000-000000000000",
    CONF_REGION: "QLD1",
    CONF_POSTCODE_PREFIX: "456",
    CONF_TOKEN: "gho_test",
}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=dict(DATA))
    entry.add_to_hass(hass)
    return entry


def _rec(ts: str) -> dict:
    return {"interval_start_utc": ts, "net_import_kw": 1.0}


async def test_buffer_and_stats_survive_restart(hass: HomeAssistant) -> None:
    """A new coordinator for the same entry restores buffer and stats."""
    entry = _entry(hass)
    c1 = NemFlexTelemetryCoordinator(hass, entry)
    c1._buffer.extend([_rec("2026-10-08T00:50:00Z"), _rec("2026-10-08T00:55:00Z")])
    c1._records_pushed_today = 11
    c1._push_error_count = 2
    c1._data.cohort_size = 2
    c1._data.last_push_time = datetime(2026, 10, 8, 0, 50, tzinfo=UTC)
    c1._roll_push_day()
    await c1._async_save_state()

    c2 = NemFlexTelemetryCoordinator(hass, entry)
    await c2.async_load_state()
    assert [r["interval_start_utc"] for r in c2._buffer] == [
        "2026-10-08T00:50:00Z",
        "2026-10-08T00:55:00Z",
    ]
    assert c2._data.records_pushed_today == 11
    assert c2._data.push_errors == 2
    assert c2._data.cohort_size == 2
    assert c2._data.last_push_time == datetime(2026, 10, 8, 0, 50, tzinfo=UTC)
    assert c2._data.buffer_size == 2


async def test_daily_counter_resets_at_local_midnight(hass: HomeAssistant) -> None:
    """The counter follows the HA time zone, not UTC."""
    await hass.config.async_set_time_zone("Australia/Brisbane")
    entry = _entry(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    c._push_day = "2026-10-07"
    c._records_pushed_today = 200
    # 2026-10-08 00:30 UTC is 10:30 AEST on 8 Oct: a new local day.
    with patch.object(
        coord_mod.dt_util, "now",
        return_value=datetime(2026, 10, 8, 10, 30).astimezone(),
    ):
        c._roll_push_day()
    assert c._records_pushed_today == 0
    assert c._push_day == "2026-10-08"


async def test_stop_saves_before_push_and_survives_timeout(hass: HomeAssistant) -> None:
    """On stop the buffer is saved even if the final push hangs."""
    entry = _entry(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    c._buffer.append(_rec("2026-10-08T00:55:00Z"))

    async def _hang() -> None:
        await asyncio.sleep(5)

    with (
        patch.object(coord_mod, "STOP_PUSH_TIMEOUT_S", 0.05),
        patch.object(c, "_async_push_buffer", side_effect=_hang),
    ):
        await c.async_handle_stop()

    c2 = NemFlexTelemetryCoordinator(hass, entry)
    await c2.async_load_state()
    assert len(c2._buffer) == 1


async def test_stop_event_triggers_handler(hass: HomeAssistant) -> None:
    """async_setup_entry registers a stop listener."""
    entry = _entry(hass)
    fake = MagicMock()
    fake.async_load_state = AsyncMock()
    fake.async_config_entry_first_refresh = AsyncMock()
    fake.async_handle_stop = AsyncMock()
    fake.async_shutdown = AsyncMock()
    fake.household_id = DATA[CONF_HOUSEHOLD_ID]
    fake.region = DATA[CONF_REGION]
    with (
        patch(
            "custom_components.nem_flex_telemetry.NemFlexTelemetryCoordinator",
            return_value=fake,
        ),
        patch("custom_components.nem_flex_telemetry.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        fake.async_load_state.assert_awaited_once()
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    fake.async_handle_stop.assert_awaited_once()


async def test_duplicate_interval_not_buffered(hass: HomeAssistant) -> None:
    """Rebuilding an interval restored from storage keeps one copy."""
    entry = _entry(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    c._buffer.append(_rec("2026-10-08T00:55:00Z"))
    with (
        patch.object(c, "_build_record", return_value=_rec("2026-10-08T00:55:00Z")),
        patch.object(c, "_validate_record", side_effect=lambda r: r),
        patch.object(c, "_async_refresh_cohort_size", AsyncMock()),
        patch.object(c, "_async_discover_context", AsyncMock()),
        patch.object(c, "_async_run_global_sweep", AsyncMock()),
        patch.object(c, "_log_power_rating_health_check"),
    ):
        await c._async_update_data()
    assert len(c._buffer) == 1


def test_dedupe_new_lines() -> None:
    """Lines already in the file are dropped; new ones kept in order."""
    existing = "\n".join(json.dumps(_rec(t)) for t in ["A", "B"]) + "\n"
    new = "\n".join(json.dumps(_rec(t)) for t in ["B", "C", "C", "D"]) + "\n"
    kept = [json.loads(x)["interval_start_utc"] for x in dedupe_new_lines(existing, new).splitlines()]
    assert kept == ["C", "D"]


class _Resp:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, resp: _Resp, calls: list) -> None:
        self._resp = resp
        self._calls = calls

    def get(self, url):
        self._calls.append(url)
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_cohort_size_reads_status_json() -> None:
    """Cohort size comes from site/data/status.json, not raw folder count."""
    calls: list[str] = []
    resp = _Resp(200, json.dumps({"cohort_size": 2}))
    client = NemFlexGitHubClient(token="t", repo_name="purcell-lab/nem-flex-telemetry")
    with patch(
        "custom_components.nem_flex_telemetry.github_client.aiohttp.ClientSession",
        return_value=_Session(resp, calls),
    ):
        assert await client.get_cohort_size() == 2
    assert calls[0].endswith("/contents/site/data/status.json")


async def test_cohort_refresh_is_rate_limited(hass: HomeAssistant) -> None:
    """Cohort size is fetched at most once per refresh period."""
    entry = _entry(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    client = MagicMock()
    client.get_cohort_size = AsyncMock(return_value=2)
    with patch.object(c, "_get_or_create_github_client", return_value=client):
        for _ in range(5):
            await c._async_refresh_cohort_size()
    assert client.get_cohort_size.await_count == 1
    assert c._data.cohort_size == 2
