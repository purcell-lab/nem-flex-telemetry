"""Rebuild from history against the real recorder (sqlite, in memory)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.nem_flex_telemetry.rebuild import (
    HistoryTimeline,
    async_fetch_history,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """The recorder database must be prepared before hass starts."""
    yield


async def test_recorder_history_is_sampled(
    recorder_mock, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """End to end against the real recorder: one batched history query."""
    t0 = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
    freezer.move_to(t0)
    hass.states.async_set("sensor.buy", "0.30")
    hass.states.async_set("sensor.sell", "0.05")
    await async_wait_recording_done(hass)
    freezer.move_to(t0 + timedelta(minutes=15, seconds=40))
    hass.states.async_set("sensor.buy", "23.0")
    await async_wait_recording_done(hass)
    freezer.move_to(t0 + timedelta(hours=1))

    history = await async_fetch_history(
        hass, ["sensor.buy", "sensor.sell", "sensor.never"],
        t0 + timedelta(minutes=5), t0 + timedelta(minutes=30),
    )
    timeline = HistoryTimeline(history)
    assert timeline.state_at("sensor.buy", t0 + timedelta(minutes=10)).state == "0.30"
    assert timeline.state_at("sensor.buy", t0 + timedelta(minutes=17)).state == "23.0"
    assert timeline.state_at("sensor.sell", t0 + timedelta(minutes=29)).state == "0.05"
    assert timeline.state_at("sensor.never", t0 + timedelta(minutes=29)) is None
    assert timeline.state_at("sensor.buy", t0 + timedelta(minutes=1)) is None


async def test_spike_interval_rebuilt_from_real_recorder(
    recorder_mock, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A missing $23/kWh interval is rebuilt from what the recorder stored."""
    from unittest.mock import patch

    from .test_rebuild import NOW, SPIKE, _client, _coordinator, _iso
    from .test_startup_readiness import REQUIRED

    freezer.move_to(SPIKE - timedelta(hours=1))
    for entity_id in REQUIRED.values():
        hass.states.async_set(entity_id, "1.5")
    await async_wait_recording_done(hass)
    freezer.move_to(SPIKE + timedelta(seconds=40))
    hass.states.async_set("sensor.buy", "23.0")
    await async_wait_recording_done(hass)
    freezer.move_to(NOW)

    c = _coordinator(hass)
    client = _client({SPIKE.date(): {_iso(SPIKE - timedelta(minutes=5))}})
    with patch.object(c, "_get_or_create_github_client", return_value=client):
        summary = await c.async_rebuild_intervals(
            start=SPIKE - timedelta(minutes=5), end=SPIKE + timedelta(minutes=5)
        )
    assert summary["already_present"] == 1
    assert summary["rebuilt_intervals"] == [_iso(SPIKE)]
    record = client.append_records.await_args.args[1][0]
    assert record["price_signal_seen"] == 23.0
    assert record["net_import_kw"] == 1.5
