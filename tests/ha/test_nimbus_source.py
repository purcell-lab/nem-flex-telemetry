"""Nimbus source: relay sensor.nimbus_flex_telemetry records (#27, nimbus#1634)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry.const import (
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_SOURCE,
    CONF_TOKEN,
    DOMAIN,
    NIMBUS_TELEMETRY_ENTITY,
    RECORDS_PER_PUSH,
    SOURCE_NIMBUS,
)
from custom_components.nem_flex_telemetry.coordinator import NemFlexTelemetryCoordinator
from custom_components.nem_flex_telemetry.nimbus_source import read_nimbus_record

SCHEMA = json.loads(
    (Path(__file__).parents[2] / "schema" / "telemetry.schema.json").read_text()
)
DATA = {
    CONF_HOUSEHOLD_ID: "00000000-0000-4000-8000-000000000000",
    CONF_REGION: "QLD1",
    CONF_POSTCODE_PREFIX: "456",
    CONF_TOKEN: "gho_test",
}


def nimbus_record(interval: str = "2026-10-08T01:00:00Z") -> dict:
    """A record shaped like nimbus flex_telemetry.build_record() output."""
    return {
        "schema_version": "2.0",
        "interval_start_utc": interval,
        "region": "QLD1",
        "postcode_prefix": "456",
        "net_import_kw": -3.2,
        "solar_kw": 9.8,
        "house_load_kw": 2.4,
        "deferrable_load_kw": 0.0,
        "naive_baseline_kw": -7.4,
        "naive_baseline_method": "subtraction",
        "price_signal_seen": 0.21,
        "price_export_seen": 0.05,
        "envelope_import_limit_kw": 42.0,
        "envelope_export_limit_kw": 40.0,
        "flex_available_up_kw": 18.0,
        "flex_available_down_kw": 21.0,
        "shadow_energy_price": 0.16,
        "shadow_load_forecast_price": None,
        "shadow_solar_forecast_price": 0.0,
        "shadow_envelope_import_price": None,
        "shadow_envelope_export_price": None,
        "assets": [
            {
                "asset_id": "home_battery",
                "kind": "stationary_battery",
                "bidirectional_capable": True,
                "capacity_kwh": 48.0,
                "soc_pct": 62.0,
                "setpoint_kw": 4.2,
                "available_up_kw": 16.8,
                "available_down_kw": 25.2,
                "shadow_power_balance_price": 0.16,
            }
        ],
        "deferrable_loads": [],
    }


class _State:
    def __init__(self, attributes: dict) -> None:
        self.state = attributes.get("record", {}).get("interval_start_utc", "unknown")
        self.attributes = attributes


def test_fixture_record_validates_against_schema() -> None:
    """The Nimbus-shaped fixture is a valid v2.0 record."""
    jsonschema.validate(nimbus_record(), SCHEMA)


def test_read_valid_record_returns_copy() -> None:
    rec = nimbus_record()
    result = read_nimbus_record(_State({"record": rec}), region="QLD1", postcode_prefix="456")
    assert result.record == rec and result.reason is None
    result.record["solar_kw"] = 0
    assert rec["solar_kw"] == 9.8


@pytest.mark.parametrize(
    ("state", "fragment"),
    [
        (None, "not found"),
        (_State({"nimbus_version": "0.94.443"}), "switch.nimbus_solver_flex_signals_enabled"),
        (_State({"reason": "ranging off"}), "Nimbus built no record: ranging off"),
        (_State({"record": {**nimbus_record(), "schema_version": "3.0"}}), "schema_version"),
        (_State({"record": {**nimbus_record(), "region": "NSW1"}}), "region"),
        (_State({"record": {**nimbus_record(), "postcode_prefix": "200"}}), "postcode prefix"),
    ],
)
def test_read_refuses_with_reason(state, fragment) -> None:
    result = read_nimbus_record(state, region="QLD1", postcode_prefix="456")
    assert result.record is None
    assert fragment in result.reason
    # Never echo the household's postcode prefix into logs.
    assert "456" not in result.reason


def _coordinator(hass: HomeAssistant) -> NemFlexTelemetryCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data=dict(DATA), options={CONF_SOURCE: SOURCE_NIMBUS}
    )
    entry.add_to_hass(hass)
    return NemFlexTelemetryCoordinator(hass, entry)


def _publish(hass: HomeAssistant, rec: dict) -> None:
    hass.states.async_set(
        NIMBUS_TELEMETRY_ENTITY, rec["interval_start_utc"], {"record": rec}
    )


async def test_poll_relays_record_once(hass: HomeAssistant) -> None:
    """The 5-minute poll buffers each Nimbus interval exactly once."""
    c = _coordinator(hass)
    with patch.object(c, "_build_record", side_effect=AssertionError("HAEO path used")):
        _publish(hass, nimbus_record("2026-10-08T01:00:00Z"))
        await c._async_update_data()
        await c._async_update_data()
        _publish(hass, nimbus_record("2026-10-08T01:05:00Z"))
        await c._async_update_data()
    assert [r["interval_start_utc"] for r in c._buffer] == [
        "2026-10-08T01:00:00Z",
        "2026-10-08T01:05:00Z",
    ]
    assert c._data.source == SOURCE_NIMBUS
    assert c._data.source_status is None


async def test_listener_relays_on_state_change(hass: HomeAssistant) -> None:
    """A Nimbus state change is relayed without waiting for the poll."""
    c = _coordinator(hass)
    assert c.async_start_nimbus_listener()
    _publish(hass, nimbus_record("2026-10-08T01:10:00Z"))
    await hass.async_block_till_done()
    assert len(c._buffer) == 1
    c.async_stop_nimbus_listener()
    c.async_stop_nimbus_listener()  # idempotent
    _publish(hass, nimbus_record("2026-10-08T01:15:00Z"))
    await hass.async_block_till_done()
    assert len(c._buffer) == 1


async def test_relayed_record_is_unchanged(hass: HomeAssistant) -> None:
    """Every Nimbus field reaches the buffer as published."""
    c = _coordinator(hass)
    rec = nimbus_record()
    _publish(hass, copy.deepcopy(rec))
    await c._async_update_data()
    assert c._buffer[0] == rec


async def test_no_record_reports_status(hass: HomeAssistant) -> None:
    """With ranging off, nothing is buffered and the reason is visible."""
    c = _coordinator(hass)
    hass.states.async_set(NIMBUS_TELEMETRY_ENTITY, "unknown", {"nimbus_version": "0.94.443"})
    await c._async_update_data()
    assert len(c._buffer) == 0
    assert "flex_signals_enabled" in c._data.source_status


async def test_full_buffer_is_pushed(hass: HomeAssistant) -> None:
    """Relayed records go out through the normal push path."""
    c = _coordinator(hass)
    push = AsyncMock()
    with patch.object(c, "_async_push_buffer", push):
        for i in range(RECORDS_PER_PUSH):
            _publish(hass, nimbus_record(f"2026-10-08T02:{i * 5:02d}:00Z"))
            await c._async_update_data()
    assert push.await_count >= 1


async def test_options_nimbus_needs_sensor(hass: HomeAssistant) -> None:
    """Choosing Nimbus without the sensor is refused."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=dict(DATA))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCE: SOURCE_NIMBUS}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCE: "nimbus_not_found"}


async def test_options_nimbus_saves_without_mappings(hass: HomeAssistant) -> None:
    """With Nimbus present, the source saves with no entity mappings."""
    hass.states.async_set(NIMBUS_TELEMETRY_ENTITY, "unknown", {})
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=dict(DATA))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCE: SOURCE_NIMBUS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SOURCE] == SOURCE_NIMBUS
