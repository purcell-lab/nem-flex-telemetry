"""Shadow-price LP duals and rejected-record handling (#30)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from homeassistant.core import HomeAssistant

from custom_components.nem_flex_telemetry.coordinator import NemFlexTelemetryCoordinator

from .test_startup_readiness import _coordinator, _set_required, _tick

SHADOW_FIELDS = [
    "shadow_energy_price",
    "shadow_load_forecast_price",
    "shadow_solar_forecast_price",
    "shadow_envelope_import_price",
    "shadow_envelope_export_price",
]
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "telemetry.schema.json").read_text()
)


def _base_record(c) -> dict:
    record = c._build_record()
    record["assets"] = [
        {**a, "shadow_power_balance_price": None} for a in record.get("assets", [])
    ]
    return record


async def _tick_with(c, record: dict) -> None:
    with patch.object(c, "_build_record", return_value=record):
        await _tick(c)


async def test_large_negative_duals_are_buffered(hass: HomeAssistant) -> None:
    """The 8 Oct episode: a full battery at the export envelope gives -9.77."""
    c = _coordinator(hass)
    _set_required(hass)
    record = _base_record(c)
    for f in SHADOW_FIELDS:
        record[f] = -9.768686
    record["shadow_solar_forecast_price"] = 9.97242068784963
    await _tick_with(c, record)
    assert len(c._buffer) == 1
    assert c._buffer[0]["shadow_energy_price"] == pytest.approx(-9.768686)
    assert c._data.validation_errors == 0


async def test_garbage_dual_is_skipped_and_counted(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad value skips the interval, counts it, and warns once per field."""
    c = _coordinator(hass)
    _set_required(hass)
    caplog.set_level(logging.WARNING)
    record = _base_record(c)
    record["shadow_energy_price"] = "abc"
    for _ in range(3):
        await _tick_with(c, dict(record))
    assert len(c._buffer) == 0
    assert c._data.validation_errors == 3
    assert c._data.push_errors == 0
    assert caplog.text.count("Skipping telemetry interval") == 1
    assert "shadow_energy_price" in caplog.text


async def test_dual_outside_guard_is_rejected(hass: HomeAssistant) -> None:
    """A $/MWh unit error (-9768.7) still fails the guard."""
    c = _coordinator(hass)
    _set_required(hass)
    record = _base_record(c)
    record["shadow_energy_price"] = -9768.686
    await _tick_with(c, record)
    assert len(c._buffer) == 0
    assert c._data.validation_errors == 1


async def test_market_price_window_unchanged(hass: HomeAssistant) -> None:
    """Market prices keep the -2.0 to 20.0 window."""
    c = _coordinator(hass)
    _set_required(hass)
    record = _base_record(c)
    record["price_signal_seen"] = 25.0
    await _tick_with(c, record)
    assert len(c._buffer) == 0
    assert c._data.validation_errors == 1


async def test_asset_power_balance_dual(hass: HomeAssistant) -> None:
    """The per-asset dual uses the same guard."""
    c = _coordinator(hass)
    _set_required(hass)
    record = _base_record(c)
    record["assets"] = [{"asset_id": "home_battery", "shadow_power_balance_price": -9.77}]
    assert c._validate_record(record)["assets"][0]["shadow_power_balance_price"] == -9.77
    record["assets"][0]["shadow_power_balance_price"] = 5000.0
    with pytest.raises(Exception):
        c._validate_record(record)


async def test_validation_errors_persist(hass: HomeAssistant) -> None:
    """The counter survives a restart, separately from push errors."""
    c1 = _coordinator(hass)
    c1._validation_error_count = 4
    await c1._async_save_state()
    c2 = NemFlexTelemetryCoordinator(hass, c1.config_entry)
    await c2.async_load_state()
    assert c2._data.validation_errors == 4
    assert c2._data.push_errors == 0


def test_json_schema_accepts_duals_and_keeps_price_window() -> None:
    """validate.yml uses this schema: duals widened, market prices unchanged."""
    props = SCHEMA["properties"]
    for f in SHADOW_FIELDS:
        assert props[f]["minimum"] == -1000.0 and props[f]["maximum"] == 1000.0
    asset = props["assets"]["items"]["properties"]["shadow_power_balance_price"]
    assert asset["minimum"] == -1000.0 and asset["maximum"] == 1000.0
    for f in ("price_signal_seen", "price_export_seen"):
        assert props[f]["minimum"] == -2.0 and props[f]["maximum"] == 20.0
    sub = jsonschema.Draft202012Validator(props["shadow_energy_price"])
    assert sub.is_valid(-9.768686)
    assert not sub.is_valid(-9768.686)
