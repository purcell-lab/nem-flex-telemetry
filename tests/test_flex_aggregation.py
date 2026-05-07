"""Unit tests for v0.5.2 flex aggregation logic.

Exercises the per-asset flex helpers and household aggregation against
Mark Purcell's reference stack: 20 kWp PV + 40 kWh battery + 30 kW hybrid
inverter + 25 kW DCEV bidirectional charger + two EVs + 30 kW grid envelope.

The expected idle outcome is:
    flex_up   = 30 kW  (battery 5 + DCEV-allocated EV 25, clipped to import 35)
    flex_down = 5 kW   (battery only; both EVs charge_only at idle, no V2G)

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
def test_battery_flex_idle_clipped_to_inverter():
    """Battery at idle: full charge + discharge headroom, inverter not binding."""
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=0.0)
    assert up == 5.0  # min(5 batt, 30 inverter) - 0 = 5
    assert down == 5.0


def test_battery_flex_charging_reduces_up_headroom():
    coord = make_coordinator(MARKS_STACK)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=2.0)  # charging at 2 kW
    assert up == pytest.approx(3.0)
    assert down == 5.0


def test_battery_flex_inverter_binds_when_smaller():
    """If the inverter is smaller than the battery, it caps charge/discharge."""
    state_map = dict(MARKS_STACK)
    state_map["number.inverter_max_ac_to_dc_power"] = 3.0
    state_map["number.inverter_max_dc_to_ac_power"] = 3.0
    coord = make_coordinator(state_map)
    up, down = coord._battery_asset_flex(battery_setpoint_kw=0.0)
    assert up == 3.0
    assert down == 3.0


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


def test_marks_stack_idle_household_flex():
    """End-to-end: Mark's stack at idle should yield flex_up=30, flex_down=5.

    - battery (idle): up=5, down=5
    - ev1 (plugged_idle, charge_only, sticky owner): up=25, down=0
    - ev2 (plugged_idle, charge_only, NOT sticky owner): up=0, down=0
    Sum: up=30, down=5; envelope (import 35, export 30) does not bind.
    """
    coord = make_coordinator(
        {
            **MARKS_STACK,
            # Envelope
            "number.grid_import_limit": 35.0,
            "number.grid_export_limit": 30.0,
        }
    )
    batt_up, batt_down = coord._battery_asset_flex(0.0)
    ev1_up, ev1_down = coord._ev_asset_flex(
        "ev1", 0.0, "plugged_idle", "charge_only"
    )
    ev2_up, ev2_down = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )

    asset_up = batt_up + ev1_up + ev2_up
    asset_down = batt_down + ev1_down + ev2_down
    envelope_import = 35.0
    envelope_export = 30.0

    flex_up = min(asset_up, envelope_import)
    flex_down = min(asset_down, envelope_export)

    assert flex_up == 30.0, f"expected 30 kW, got {flex_up}"
    assert flex_down == 5.0, f"expected 5 kW (no V2G at idle), got {flex_down}"


def test_marks_stack_idle_with_v2g_active():
    """When ev1 actively discharging (bidirectional), down should reach 30 kW.

    - battery (idle): up=5, down=5
    - ev1 (discharging, bidirectional): up=25, down=25
    - ev2: up=0, down=0
    Sum: up=30, down=30; export envelope (30) binds at exactly 30.
    """
    coord = make_coordinator(MARKS_STACK)
    batt_up, batt_down = coord._battery_asset_flex(0.0)
    ev1_up, ev1_down = coord._ev_asset_flex(
        "ev1", 0.0, "discharging", "bidirectional"
    )
    ev2_up, ev2_down = coord._ev_asset_flex(
        "ev2", 0.0, "plugged_idle", "charge_only"
    )

    flex_up = min(batt_up + ev1_up + ev2_up, 35.0)
    flex_down = min(batt_down + ev1_down + ev2_down, 30.0)

    assert flex_up == 30.0
    assert flex_down == 30.0  # full export envelope used
