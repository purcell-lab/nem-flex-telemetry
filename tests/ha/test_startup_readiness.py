"""Startup readiness: no zero records, no false fallback warnings (#21)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry.const import (
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_DCEV_AC_TO_DC,
    ENTITY_DCEV_DC_TO_AC,
    ENTITY_INVERTER_AC_TO_DC,
    ENTITY_INVERTER_DC_TO_AC,
)
from custom_components.nem_flex_telemetry.coordinator import (
    STARTUP_GRACE_INTERVALS,
    NemFlexTelemetryCoordinator,
)

REQUIRED = {
    CONF_ENTITY_NET_IMPORT: "sensor.grid",
    CONF_ENTITY_SOLAR: "sensor.pv",
    CONF_ENTITY_TOTAL_LOAD: "sensor.load",
    CONF_ENTITY_PRICE_SIGNAL: "sensor.buy",
    CONF_ENTITY_PRICE_EXPORT: "sensor.sell",
}
RATINGS = [
    ENTITY_BATTERY_MAX_CHARGE, ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_INVERTER_AC_TO_DC, ENTITY_INVERTER_DC_TO_AC,
    ENTITY_DCEV_AC_TO_DC, ENTITY_DCEV_DC_TO_AC,
]


def _coordinator(hass: HomeAssistant) -> NemFlexTelemetryCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_HOUSEHOLD_ID: "00000000-0000-4000-8000-000000000000",
            CONF_REGION: "NSW1",
            CONF_POSTCODE_PREFIX: "200",
            CONF_TOKEN: "t",
            **REQUIRED,
        },
    )
    entry.add_to_hass(hass)
    return NemFlexTelemetryCoordinator(hass, entry)


def _set_required(hass: HomeAssistant, value: str = "1.0") -> None:
    for entity_id in REQUIRED.values():
        hass.states.async_set(entity_id, value)


async def _tick(c: NemFlexTelemetryCoordinator) -> None:
    with (
        patch.object(c, "_async_discover_context", AsyncMock()),
        patch.object(c, "_async_run_global_sweep", AsyncMock()),
        patch.object(c, "_async_push_buffer", AsyncMock()),
    ):
        c._context_discovered = True
        await c._async_update_data()


async def test_unavailable_required_input_skips_interval(hass: HomeAssistant) -> None:
    """No record is buffered while a required input is unavailable."""
    c = _coordinator(hass)
    _set_required(hass)
    hass.states.async_set("sensor.load", "unavailable")
    await _tick(c)
    assert len(c._buffer) == 0
    assert c._data.skipped_intervals == 1


async def test_record_buffered_once_inputs_ready(hass: HomeAssistant) -> None:
    """Once all required inputs have numeric states the interval is kept."""
    c = _coordinator(hass)
    _set_required(hass)
    await _tick(c)
    assert len(c._buffer) == 1


async def test_unmapped_required_input_skips(hass: HomeAssistant) -> None:
    """A required field with no mapping is reported, not zero-filled."""
    c = _coordinator(hass)
    _set_required(hass)
    c._config[CONF_ENTITY_TOTAL_LOAD] = None
    assert c._missing_required_inputs() == ["entity_total_load_kw=not mapped"]


async def test_missing_input_warns_after_grace_only(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing inputs are DEBUG during startup grace, then one WARNING."""
    c = _coordinator(hass)
    _set_required(hass)
    hass.states.async_set("sensor.load", "unknown")
    caplog.set_level(logging.DEBUG)
    for _ in range(STARTUP_GRACE_INTERVALS):
        await _tick(c)
    assert "Skipping telemetry interval" not in caplog.text
    for _ in range(3):
        await _tick(c)
    assert caplog.text.count("Skipping telemetry interval") == 1


async def test_late_rating_entity_is_not_reported_as_fallback(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Ratings that publish during startup grace produce no WARNING."""
    c = _coordinator(hass)
    caplog.set_level(logging.DEBUG)
    c._intervals_seen = 1
    c._log_power_rating_health_check()  # nothing published yet
    for entity_id in RATINGS:
        hass.states.async_set(entity_id, "21.0")
    c._intervals_seen = 2
    c._log_power_rating_health_check()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    assert "6 live, 0 fallback" in caplog.text


async def test_rating_still_missing_after_grace_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A rating still missing after grace is a single WARNING."""
    c = _coordinator(hass)
    for entity_id in RATINGS[1:]:
        hass.states.async_set(entity_id, "21.0")
    caplog.set_level(logging.INFO)
    for n in range(STARTUP_GRACE_INTERVALS + 1, STARTUP_GRACE_INTERVALS + 4):
        c._intervals_seen = n
        c._log_power_rating_health_check()
    fallbacks = [r for r in caplog.records if "FALLBACK" in r.getMessage()]
    assert len(fallbacks) == 1
    assert ENTITY_BATTERY_MAX_CHARGE in fallbacks[0].getMessage()
