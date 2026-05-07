"""Unit tests for v0.5.3 flex aggregation logic.

Exercises the per-asset flex helpers and household aggregation against
Mark Purcell's reference stack: 20 kWp PV + 40 kWh battery + 30 kW hybrid
inverter + 25 kW DCEV bidirectional charger + two EVs + 30 kW grid envelope.

Topology: hybrid inverter is the AC bus for everything (battery + PV + DCEV).
DCEV is a DC-DC converter on the DC bus, not a separate AC inverter. So the
household flex ceiling layers as:

    flex = min( sum(asset.available_*),
                hybrid inverter rating,
                grid envelope import/export limit )

For Mark's stack at idle (battery 5 kW rating, both EVs plugged_idle/charge_only):
    asset_sum_up   = 5 (batt) + 25 (DCEV-allocated EV) + 0 = 30
    asset_sum_down = 5 (batt) +  0 (no V2G)            + 0 =  5
    flex_up   = min(30, 30, 35) = 30 kW   (asset sum and inverter both bind)
    flex_down = min( 5, 30, 30) =  5 kW   (battery alone binds, no V2G active)

Tests are deliberately structural: they exercise the pure flex helpers via
a minimal stub of the coordinator's hass.states dependency, without booting
the full Home Assistant test framework.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Home Assistant stub: just enough for the helpers under test to import.
# ---------------------------------------------------------------------------
def _install_ha_stubs() -> None:
    """Install minimal stubs for homeassistant.* modules used at import time."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )
    config_entries = types.ModuleType("homeassistant.config_entries")
    const_mod = types.ModuleType("homeassistant.const")

    class HomeAssistant:  # noqa: D401 - stub
        """Stub HomeAssistant."""

    class ServiceCall:  # noqa: D401 - stub
        pass

    class DataUpdateCoordinator:  # noqa: D401 - stub
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __class_getitem__(cls, item):  # noqa: D401
            return cls

    class UpdateFailed(Exception):  # noqa: D401 - stub
        pass

    class ConfigEntry:  # noqa: D401 - stub
        pass

    core.HomeAssistant = HomeAssistant
    core.ServiceCall = ServiceCall
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    config_entries.ConfigEntry = ConfigEntry
    helpers.update_coordinator = update_coordinator

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.const"] = const_mod


_install_ha_stubs()

# Stub aiohttp (only imported for type hints)
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
# voluptuous schema validators are referenced at module import
try:
    import voluptuous  # noqa: F401
except ImportError:  # pragma: no cover - install if missing
    pytest.skip("voluptuous not installed", allow_module_level=True)

# Stub the .github_client and .discovery siblings that coordinator imports.
github_client = types.ModuleType(
    "custom_components.nem_flex_telemetry.github_client"
)


class _Stub(Exception):
    pass


github_client.GitHubPushError = _Stub
github_client.TokenInvalidError = _Stub
github_client.NemFlexGitHubClient = MagicMock
sys.modules[
    "custom_components.nem_flex_telemetry.github_client"
] = github_client

discovery = types.ModuleType("custom_components.nem_flex_telemetry.discovery")
discovery.discover_context_entities = lambda *a, **k: {}
discovery.run_global_sweep = lambda *a, **k: {}
sys.modules["custom_components.nem_flex_telemetry.discovery"] = discovery

# Now safe to import the coordinator module.
from custom_components.nem_flex_telemetry import coordinator as coord_module  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures: a fake hass with a controllable states dict.
# ---------------------------------------------------------------------------
class FakeState:
    def __init__(self, value: float | str) -> None:
        self.state = str(value)


class FakeStates:
    def __init__(self, mapping: dict[str, FakeState]) -> None:
        self._mapping = mapping

    def get(self, entity_id: str):
        return self._mapping.get(entity_id)


class FakeHass:
    def __init__(self, mapping: dict[str, float | str]) -> None:
        self.states = FakeStates(
            {k: FakeState(v) for k, v in mapping.items()}
        )


def make_coordinator(state_map: dict[str, float | str]):
    """Build a coordinator instance with the helper methods bound but no DB."""
    coord = coord_module.NemFlexTelemetryCoordinator.__new__(
        coord_module.NemFlexTelemetryCoordinator
    )
    coord.hass = FakeHass(state_map)
    coord._flex_derived_logged = False
    coord._power_ratings_logged = False
    coord._last_bidirectional_ev_id = None
    return coord


MARKS_STACK = {
    # Battery and hybrid inverter (Mark's reference stack)
    "number.battery_max_charge_power": 5.0,
    "number.battery_max_discharge_power": 5.0,
    "number.inverter_max_ac_to_dc_power": 30.0,
    "number.inverter_max_dc_to_ac_power": 30.0,
    # DCEV bidirectional charger
    "number.dcev_inverter_max_ac_to_dc_power": 25.0,
    "number.dcev_inverter_max_dc_to_ac_power": 25.0,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_battery_flex_idle_returns_raw_rating():
    """Battery at idle reports its raw DC-side rating (no inverter clip here)."""
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=0.0)
    assert up == 5.0  # number.battery_max_charge_power
    assert down == 5.0  # number.battery_max_discharge_power


def test_battery_flex_charging_reduces_up_headroom():
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=2.0)  # charging at 2 kW
    assert up == pytest.approx(3.0)
    assert down == 5.0


def test_battery_flex_ignores_inverter_per_asset():
    """Per-asset battery flex must NOT clip to inverter; that's a household ceiling.

    Even if the inverter is tiny (3 kW), per-asset reports the battery's own
    5 kW rating. The inverter clip is applied later in _build_record.
    """
    state_map = dict(MARKS_STACK)
    state_map["number.inverter_max_ac_to_dc_power"] = 3.0
    state_map["number.inverter_max_dc_to_ac_power"] = 3.0
    coord = make_coordinator(state_map)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=0.0)
    assert up == 5.0  # raw battery rating, not clipped here
    assert down == 5.0


def test_battery_flex_reads_live_high_rating():
    """If number.battery_max_*_power reports e.g. 10 kW, that's what we use."""
    state_map = dict(MARKS_STACK)
    state_map["number.battery_max_charge_power"] = 10.0
    state_map["number.battery_max_discharge_power"] = 10.0
    coord = make_coordinator(state_map)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=0.0)
    assert up == 10.0
    assert down == 10.0


def test_ev_flex_unplugged_is_zero():
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._ev_asset_flex(
        "ev1", 0.0, "unplugged", "none"
    )
    assert (up, down) == (0.0, 0.0)


def test_ev_flex_charge_only_no_v2g():
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "charge_only"
    )
    assert up == 25.0  # full DCEV charge headroom
    assert down == 0.0  # no V2G


def test_ev_flex_bidirectional_full_headroom():
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "bidirectional"
    )
    assert up == 25.0
    assert down == 25.0


def test_dcev_allocator_only_one_ev_at_a_time():
    """The shared DCEV charger should be allocated to one EV; the other gets 0."""
    coord = make_coordinator(MARKS_STACK)
    up1, _ = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "charge_only"
    )
    up2, _ = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )
    assert up1 == 25.0
    assert up2 == 0.0  # ev1 is sticky owner; ev2 gets nothing
    assert coord._last_bidirectional_ev_id == "ev1"


def test_dcev_allocator_respects_existing_sticky_owner():
    """If ev2 was the previous bidirectional user, ev1 yields to it."""
    coord = make_coordinator(MARKS_STACK)
    coord._last_bidirectional_ev_id = "ev2"
    up1, _ = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "charge_only"
    )
    up2, _ = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )
    assert up1 == 0.0
    assert up2 == 25.0


# ---------------------------------------------------------------------------
# Helper: replicate the _build_record household aggregation in pure form.
# Mirrors the layered min() in coordinator.py so tests don't need to mock the
# full _build_record path.
# ---------------------------------------------------------------------------
def _household_flex(coord, asset_records, envelope_import, envelope_export):
    asset_up_sum = sum(a["available_up_kw"] for a in asset_records)
    asset_down_sum = sum(a["available_down_kw"] for a in asset_records)
    inverter_up = coord._read_inverter_ac_to_dc()
    inverter_down = coord._read_inverter_dc_to_ac()
    return (
        min(asset_up_sum, inverter_up, envelope_import),
        min(asset_down_sum, inverter_down, envelope_export),
    )


def _idle_assets(coord):
    """Build the three asset records for Mark's stack at idle."""
    batt_up, batt_down = coord._battery_asset_flex(0.0)
    ev1_up, ev1_down = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "charge_only"
    )
    ev2_up, ev2_down = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )
    return [
        {"available_up_kw": batt_up, "available_down_kw": batt_down},
        {"available_up_kw": ev1_up, "available_down_kw": ev1_down},
        {"available_up_kw": ev2_up, "available_down_kw": ev2_down},
    ]


def test_marks_stack_idle_household_flex():
    """End-to-end: Mark's stack at idle should yield flex_up=30, flex_down=5.

    Layered ceiling: asset_sum (30/5) <= inverter (30/30) <= envelope (35/30).
    Asset_sum and inverter are tied for flex_up; battery alone binds flex_down.
    """
    coord = make_coordinator(MARKS_STACK)
    flex_up, flex_down = _household_flex(
        coord, _idle_assets(coord), envelope_import=35.0, envelope_export=30.0
    )
    assert flex_up == 30.0, f"expected 30 kW, got {flex_up}"
    assert flex_down == 5.0, f"expected 5 kW (no V2G at idle), got {flex_down}"


def test_marks_stack_idle_with_v2g_active():
    """V2G dispatching: flex_down rises from 5 to 30 kW.

    - battery (idle): up=5, down=5
    - ev1 (discharging, bidirectional): up=25, down=25
    - ev2: up=0, down=0
    asset_sum: up=30, down=30. Inverter (30) and export envelope (30) tied.
    """
    coord = make_coordinator(MARKS_STACK)
    batt_up, batt_down = coord._battery_asset_flex(0.0)
    ev1_up, ev1_down = coord._ev_asset_flex(
        "ev1", 0.0, "discharging", "bidirectional"
    )
    ev2_up, ev2_down = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )
    assets = [
        {"available_up_kw": batt_up, "available_down_kw": batt_down},
        {"available_up_kw": ev1_up, "available_down_kw": ev1_down},
        {"available_up_kw": ev2_up, "available_down_kw": ev2_down},
    ]
    flex_up, flex_down = _household_flex(
        coord, assets, envelope_import=35.0, envelope_export=30.0
    )
    assert flex_up == 30.0
    assert flex_down == 30.0


def test_hybrid_inverter_binds_when_smallest():
    """If hybrid inverter is smaller than asset_sum and envelope, IT binds.

    Asset sum at idle = 30 (battery 5 + DCEV-allocated EV 25). With a 12 kW
    hybrid inverter and a generous 35 kW envelope, the inverter is the binding
    constraint at 12 kW (not the assets, not the grid).
    """
    state_map = dict(MARKS_STACK)
    state_map["number.inverter_max_ac_to_dc_power"] = 12.0
    state_map["number.inverter_max_dc_to_ac_power"] = 12.0
    coord = make_coordinator(state_map)
    flex_up, flex_down = _household_flex(
        coord, _idle_assets(coord), envelope_import=35.0, envelope_export=30.0
    )
    assert flex_up == 12.0  # inverter binds, beating asset sum (30) and envelope (35)
    assert flex_down == 5.0  # asset sum still binds (battery only at idle)


def test_grid_envelope_binds_when_smallest():
    """Tight CSIP-AUS export envelope (e.g. 8 kW) binds even with V2G active."""
    coord = make_coordinator(MARKS_STACK)
    batt_up, batt_down = coord._battery_asset_flex(0.0)
    ev1_up, ev1_down = coord._ev_asset_flex(
        "ev1", 0.0, "discharging", "bidirectional"
    )
    ev2_up, ev2_down = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )
    assets = [
        {"available_up_kw": batt_up, "available_down_kw": batt_down},
        {"available_up_kw": ev1_up, "available_down_kw": ev1_down},
        {"available_up_kw": ev2_up, "available_down_kw": ev2_down},
    ]
    # Tight 8 kW CSIP-AUS export envelope
    flex_up, flex_down = _household_flex(
        coord, assets, envelope_import=35.0, envelope_export=8.0
    )
    assert flex_up == 30.0
    assert flex_down == 8.0  # envelope binds, even though assets could deliver 30


# ---------------------------------------------------------------------------
# Power-rating health-check (one-shot startup logging)
# ---------------------------------------------------------------------------
def test_health_check_all_live(caplog):
    """All six number.* power-rating entities present -> 6 live, 0 fallback."""
    import logging
    coord = make_coordinator(MARKS_STACK)
    with caplog.at_level(logging.INFO, logger="custom_components.nem_flex_telemetry.coordinator"):
        coord._log_power_rating_health_check()
    text = caplog.text
    assert "6 live, 0 fallback" in text
    assert "battery max charge -> 5.0 kW (live" in text
    assert "hybrid inverter AC->DC -> 30.0 kW (live" in text
    assert "DCEV charger AC->DC -> 25.0 kW (live" in text
    assert "FALLBACK" not in text
    # Re-running must be a no-op (gate honoured).
    caplog.clear()
    coord._log_power_rating_health_check()
    assert caplog.text == ""


def test_health_check_logs_fallback_when_entity_missing(caplog):
    """If a number.* entity is missing, log a WARNING with FALLBACK marker."""
    import logging
    state_map = dict(MARKS_STACK)
    del state_map["number.battery_max_charge_power"]
    del state_map["number.dcev_inverter_max_dc_to_ac_power"]
    coord = make_coordinator(state_map)
    with caplog.at_level(logging.INFO, logger="custom_components.nem_flex_telemetry.coordinator"):
        coord._log_power_rating_health_check()
    text = caplog.text
    assert "4 live, 2 fallback" in text
    # Battery charge falls back to default (30.0 in v0.5.3).
    assert "battery max charge -> 30.0 kW (FALLBACK" in text
    assert "DCEV charger DC->AC -> 25.0 kW (FALLBACK" in text


def test_health_check_treats_unavailable_as_fallback(caplog):
    """An entity reporting 'unavailable' must trigger the fallback path."""
    import logging
    state_map = dict(MARKS_STACK)
    state_map["number.inverter_max_ac_to_dc_power"] = "unavailable"
    coord = make_coordinator(state_map)
    with caplog.at_level(logging.INFO, logger="custom_components.nem_flex_telemetry.coordinator"):
        coord._log_power_rating_health_check()
    assert "hybrid inverter AC->DC -> 30.0 kW (FALLBACK" in caplog.text
    assert "5 live, 1 fallback" in caplog.text
