"""Variable battery / EV asset list (#15)."""
from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry import async_migrate_entry
from custom_components.nem_flex_telemetry.config_flow import NemFlexTelemetryConfigFlow
from custom_components.nem_flex_telemetry.const import (
    ASSET_DEFAULTS,
    CONF_ASSETS,
    CONF_BIDIRECTIONAL_CHARGERS,
    CONF_EV_COUNT,
    CONF_HOME_BATTERY_COUNT,
    DOMAIN,
    ENTITY_DCEV_AC_TO_DC,
)
from custom_components.nem_flex_telemetry.coordinator import (
    STARTUP_GRACE_INTERVALS,
    NemFlexTelemetryCoordinator,
)

from .test_options_flow import BASE_DATA, _all_mapped
from .test_startup_readiness import REQUIRED, _set_required

BATTERY = {
    "asset_id": "home_battery",
    "kind": "stationary_battery",
    "capacity_kwh": 13.5,
    "bidirectional_capable": True,
    "soc_entity": "sensor.batt_soc",
    "setpoint_entity": "sensor.batt_power",
    "shadow_entity": None,
}


def _ev(n: int, capacity: float = 60.0, bidirectional: bool = True) -> dict:
    return {
        "asset_id": f"ev{n}",
        "kind": "ev",
        "capacity_kwh": capacity,
        "bidirectional_capable": bidirectional,
        "soc_entity": f"sensor.car{n}_soc",
        "setpoint_entity": f"sensor.car{n}_power",
        "shadow_entity": f"sensor.car{n}_shadow",
    }


# ---------------------------------------------------------------------------
# Coordinator: only configured, existing assets are published
# ---------------------------------------------------------------------------
def _coordinator(
    hass: HomeAssistant, assets: list[dict], chargers: int = 1
) -> NemFlexTelemetryCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={
            **BASE_DATA,
            **REQUIRED,
            CONF_ASSETS: assets,
            CONF_BIDIRECTIONAL_CHARGERS: chargers,
        },
    )
    entry.add_to_hass(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    c._intervals_seen = STARTUP_GRACE_INTERVALS + 1
    return c


def _publish(hass: HomeAssistant, assets: list[dict]) -> None:
    _set_required(hass)
    for a in assets:
        hass.states.async_set(a["soc_entity"], "50")
        hass.states.async_set(a["setpoint_entity"], "0")
        if a["shadow_entity"]:
            hass.states.async_set(a["shadow_entity"], "0.1")


@pytest.mark.parametrize(
    ("assets", "expected"),
    [
        ([BATTERY], ["home_battery"]),                          # 0 EV
        ([BATTERY, _ev(1)], ["home_battery", "ev1"]),           # 1 EV
        ([BATTERY, _ev(1), _ev(2)], ["home_battery", "ev1", "ev2"]),  # 2 EV
        ([_ev(1)], ["ev1"]),                                    # no battery
        ([], []),                                               # nothing at all
    ],
    ids=["0ev", "1ev", "2ev", "no-battery", "none"],
)
async def test_record_has_configured_assets_only(
    hass: HomeAssistant, assets: list[dict], expected: list[str]
) -> None:
    c = _coordinator(hass, assets)
    _publish(hass, assets)
    record = c._build_record()
    assert [a["asset_id"] for a in record["assets"]] == expected
    for a in record["assets"]:
        assert a["soc_pct"] == 50.0
    c._validate_record(record)


async def test_no_battery_household_flex_is_ev_only(hass: HomeAssistant) -> None:
    """Without a battery there is no 30 kW battery fallback in household flex."""
    ev = _ev(1)
    c = _coordinator(hass, [ev], chargers=0)
    _publish(hass, [ev])
    hass.states.async_set(ev["shadow_entity"], "unavailable")  # unplugged
    record = c._build_record()
    assert record["flex_available_up_kw"] == 0.0
    assert record["flex_available_down_kw"] == 0.0


async def test_one_ev_without_bidirectional_charger(hass: HomeAssistant) -> None:
    ev = _ev(1, bidirectional=False)
    c = _coordinator(hass, [ev], chargers=0)
    _publish(hass, [ev])
    (asset,) = c._build_record()["assets"]
    assert asset["bidirectional_capable"] is False
    assert asset["connection_state"] == "plugged_idle"
    assert asset["available_down_kw"] == 0.0
    assert asset["available_up_kw"] > 0.0


async def test_two_evs_two_chargers_both_get_flex(hass: HomeAssistant) -> None:
    assets = [_ev(1), _ev(2)]
    c = _coordinator(hass, assets, chargers=2)
    _publish(hass, assets)
    hass.states.async_set("number.dcev_inverter_max_ac_to_dc_power", "11")
    record = c._build_record()
    assert [a["available_up_kw"] for a in record["assets"]] == [11.0, 11.0]


async def test_missing_asset_skipped_and_logged_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    assets = [BATTERY, _ev(1)]
    c = _coordinator(hass, assets)
    _publish(hass, [BATTERY])  # ev1 entities never created
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        record = c._build_record()
        assert [a["asset_id"] for a in record["assets"]] == ["home_battery"]
    assert caplog.text.count("Asset ev1 left out of telemetry") == 1

    # It comes back once its entity exists.
    caplog.set_level(logging.INFO)
    _publish(hass, [_ev(1)])
    assert [a["asset_id"] for a in c._build_record()["assets"]] == ["home_battery", "ev1"]
    assert "Asset ev1 is available again" in caplog.text


async def test_missing_asset_quiet_during_startup_grace(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    c = _coordinator(hass, [BATTERY])
    c._intervals_seen = 1
    _set_required(hass)
    caplog.set_level(logging.WARNING)
    assert c._build_record()["assets"] == []
    assert "left out of telemetry" not in caplog.text


async def test_legacy_entry_without_asset_list_keeps_reference_trio(
    hass: HomeAssistant,
) -> None:
    """A v3 entry (no CONF_ASSETS) still publishes home_battery, ev1, ev2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={**BASE_DATA, **REQUIRED, "ev1_capacity_kwh": 70.0},
    )
    entry.add_to_hass(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    _set_required(hass)
    for spec in ASSET_DEFAULTS.values():
        hass.states.async_set(spec["soc_entity"], "40")
    record = c._build_record()
    assert [a["asset_id"] for a in record["assets"]] == ["home_battery", "ev1", "ev2"]
    assert [a["capacity_kwh"] for a in record["assets"]] == [13.5, 70.0, 60.0]


# ---------------------------------------------------------------------------
# Migration v3 -> v4
# ---------------------------------------------------------------------------
async def test_migration_keeps_reference_install(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            **BASE_DATA,
            "home_battery_capacity_kwh": 40.0,
            "ev1_capacity_kwh": 75.0,
            "ev2_capacity_kwh": 60.0,
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert entry.data[CONF_BIDIRECTIONAL_CHARGERS] == 1
    assert "ev1_capacity_kwh" not in entry.data
    assets = entry.data[CONF_ASSETS]
    assert [a["asset_id"] for a in assets] == ["home_battery", "ev1", "ev2"]
    assert [a["capacity_kwh"] for a in assets] == [40.0, 75.0, 60.0]
    for a in assets:
        spec = ASSET_DEFAULTS[a["asset_id"]]
        assert a["soc_entity"] == spec["soc_entity"]
        assert a["setpoint_entity"] == spec["setpoint_entity"]
        assert a["shadow_entity"] == spec["shadow_entity"]


async def test_migration_drops_placeholder_and_reads_options(hass: HomeAssistant) -> None:
    """The one-EV tester typed 0.1 kWh for EV2; capacities edited in options win."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={**BASE_DATA, "ev2_capacity_kwh": 60.0},
        options={"home_battery_capacity_kwh": 33.14, "ev2_capacity_kwh": 0.1},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert [a["asset_id"] for a in entry.data[CONF_ASSETS]] == ["home_battery", "ev1"]
    assert entry.data[CONF_ASSETS][0]["capacity_kwh"] == 33.14
    assert "ev2_capacity_kwh" not in entry.options


# ---------------------------------------------------------------------------
# Config flow asset steps
# ---------------------------------------------------------------------------
def _flow(hass: HomeAssistant) -> NemFlexTelemetryConfigFlow:
    flow = NemFlexTelemetryConfigFlow()
    flow.hass = hass
    flow._unmapped_entities = []
    return flow


def _counts(batteries: int, evs: int, chargers: int = 1) -> dict:
    return {
        CONF_HOME_BATTERY_COUNT: batteries,
        CONF_EV_COUNT: evs,
        CONF_BIDIRECTIONAL_CHARGERS: chargers,
    }


async def _run_asset_steps(flow, counts: dict, answers: list[dict]):
    result = await flow.async_step_assets(counts)
    for answer in answers:
        assert result["type"] is FlowResultType.FORM
        step = getattr(flow, f"async_step_{result['step_id']}")
        result = await step(answer)
    return result


@pytest.mark.parametrize(
    ("batteries", "evs", "expected"),
    [
        (1, 0, ["home_battery"]),
        (1, 1, ["home_battery", "ev1"]),
        (1, 2, ["home_battery", "ev1", "ev2"]),
        (0, 1, ["ev1"]),
        (0, 0, []),
    ],
    ids=["0ev", "1ev", "2ev", "no-battery", "none"],
)
async def test_config_flow_variable_assets(
    hass: HomeAssistant, batteries: int, evs: int, expected: list[str]
) -> None:
    flow = _flow(hass)
    answers = [
        {"capacity_kwh": 10.0 + i, "soc_entity": f"sensor.asset{i}_soc"}
        for i in range(batteries + evs)
    ]
    result = await _run_asset_steps(flow, _counts(batteries, evs), answers)
    assert result["step_id"] == "consent"
    assets = flow._data[CONF_ASSETS]
    assert [a["asset_id"] for a in assets] == expected
    assert [a["capacity_kwh"] for a in assets] == [10.0 + i for i in range(len(expected))]
    assert flow._data[CONF_BIDIRECTIONAL_CHARGERS] == 1


async def test_config_flow_zero_capacity_skips_asset(hass: HomeAssistant) -> None:
    flow = _flow(hass)
    answers = [
        {"capacity_kwh": 13.5, "soc_entity": "sensor.batt_soc"},
        {"capacity_kwh": 75.0, "soc_entity": "sensor.ev1_soc"},
        {"capacity_kwh": 0},  # EV2 does not exist
    ]
    await _run_asset_steps(flow, _counts(1, 2), answers)
    assert [a["asset_id"] for a in flow._data[CONF_ASSETS]] == ["home_battery", "ev1"]


async def test_config_flow_capacity_needs_soc_entity(hass: HomeAssistant) -> None:
    flow = _flow(hass)
    result = await _run_asset_steps(flow, _counts(1, 0), [{"capacity_kwh": 13.5}])
    assert result["step_id"] == "asset_battery"
    assert result["errors"] == {"soc_entity": "entity_required"}


async def test_config_flow_no_chargers_means_charge_only(hass: HomeAssistant) -> None:
    flow = _flow(hass)
    result = await flow.async_step_assets(_counts(0, 1, chargers=0))
    assert result["step_id"] == "asset_ev"
    assert "bidirectional_capable" not in result["data_schema"].schema
    await flow.async_step_asset_ev({"capacity_kwh": 60.0, "soc_entity": "sensor.ev_soc"})
    assert flow._data[CONF_ASSETS][0]["bidirectional_capable"] is False
    assert flow._data[CONF_BIDIRECTIONAL_CHARGERS] == 0


async def test_config_flow_prefills_from_discovery(hass: HomeAssistant) -> None:
    """Counts and mappings come from hinted entities that exist."""
    hass.states.async_set(ASSET_DEFAULTS["home_battery"]["soc_entity"], "50")
    hass.states.async_set(ASSET_DEFAULTS["ev1"]["soc_entity"], "50")
    hass.states.async_set(ASSET_DEFAULTS["ev1"]["shadow_entity"], "0.1")
    hass.states.async_set(ENTITY_DCEV_AC_TO_DC, "25")
    flow = _flow(hass)
    result = await flow.async_step_assets()
    schema = result["data_schema"]({})
    assert schema == _counts(1, 1, chargers=1)

    result = await flow.async_step_assets(_counts(0, 1))
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested["soc_entity"] == ASSET_DEFAULTS["ev1"]["soc_entity"]
    assert suggested["shadow_entity"] == ASSET_DEFAULTS["ev1"]["shadow_entity"]
    assert suggested["setpoint_entity"] is None  # hinted entity does not exist
    assert suggested["capacity_kwh"] == ASSET_DEFAULTS["ev1"]["capacity_kwh"]


async def test_config_flow_nothing_discovered_defaults_to_zero(hass: HomeAssistant) -> None:
    flow = _flow(hass)
    result = await flow.async_step_assets()
    assert result["data_schema"]({}) == _counts(0, 0, chargers=0)


# ---------------------------------------------------------------------------
# Options flow: edit the asset list
# ---------------------------------------------------------------------------
async def test_options_flow_edits_assets(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={
            **BASE_DATA,
            CONF_ASSETS: [BATTERY, _ev(1), _ev(2)],
            CONF_BIDIRECTIONAL_CHARGERS: 1,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**_all_mapped(), "edit_assets": True}
    )
    assert result["step_id"] == "assets"
    # The second EV has gone; the battery stays.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_counts(1, 1, chargers=1)
    )
    assert result["step_id"] == "asset_battery"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"capacity_kwh": 20.0, "soc_entity": "sensor.batt_soc"},
    )
    assert result["step_id"] == "asset_ev"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "capacity_kwh": 60.0,
            "soc_entity": "sensor.car1_soc",
            "bidirectional_capable": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assets = entry.options[CONF_ASSETS]
    assert [a["asset_id"] for a in assets] == ["home_battery", "ev1"]
    assert assets[0]["capacity_kwh"] == 20.0
    assert assets[1]["bidirectional_capable"] is False
    assert entry.options[CONF_BIDIRECTIONAL_CHARGERS] == 1
