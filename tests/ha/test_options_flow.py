"""Options flow regression tests for issue #16."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry.const import (
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SHADOW_ENERGY,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
)

BASE_DATA = {
    CONF_HOUSEHOLD_ID: "00000000-0000-4000-8000-000000000000",
    CONF_REGION: "NSW1",
    CONF_POSTCODE_PREFIX: "200",
    CONF_TOKEN: "gho_test",
    CONF_ENTITY_NET_IMPORT: "sensor.grid_power",
    CONF_ENTITY_SOLAR: "sensor.pv_power",
    CONF_ENTITY_TOTAL_LOAD: "sensor.wrong_load",
    CONF_ENTITY_PRICE_SIGNAL: "sensor.buy_price",
    CONF_ENTITY_PRICE_EXPORT: "sensor.sell_price",
}


def _entry(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data=dict(BASE_DATA), options=options
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_flow_opens(hass: HomeAssistant) -> None:
    """Opening options must show a form, not raise (the reported 500)."""
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


def _all_mapped() -> dict[str, str]:
    from custom_components.nem_flex_telemetry.const import DEFAULT_ENTITY_MAPPINGS

    return {
        field: BASE_DATA.get(field, f"sensor.{field}")
        for field in DEFAULT_ENTITY_MAPPINGS
    }


async def test_options_save_writes_options(hass: HomeAssistant) -> None:
    """Saving writes every entity field and the asset list to entry.options."""
    entry = _entry(hass, home_battery_capacity_kwh=33.14)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        **_all_mapped(),
        CONF_ENTITY_TOTAL_LOAD: "sensor.house_load",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENTITY_TOTAL_LOAD] == "sensor.house_load"
    # Without "Edit batteries and EVs" the asset list is carried over (#15).
    battery = entry.options["assets"][0]
    assert battery["asset_id"] == "home_battery"
    assert battery["capacity_kwh"] == 33.14
    # entry.data is untouched; the setup record is preserved.
    assert entry.data[CONF_ENTITY_TOTAL_LOAD] == "sensor.wrong_load"


async def test_options_accept_number_entities(hass: HomeAssistant) -> None:
    """HAEO inputs are number.* entities; the selector must accept them."""
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        **_all_mapped(),
        CONF_ENTITY_SOLAR: "number.solar_forecast",
        CONF_ENTITY_PRICE_SIGNAL: "number.grid_import_price",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENTITY_SOLAR] == "number.solar_forecast"


async def test_options_optional_field_can_be_cleared(hass: HomeAssistant) -> None:
    """A cleared optional field is stored as None and overrides entry.data."""
    entry = _entry(hass)
    entry_data = {**entry.data, CONF_ENTITY_SHADOW_ENERGY: "sensor.old_shadow"}
    hass.config_entries.async_update_entry(entry, data=entry_data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = _all_mapped()
    user_input.pop(CONF_ENTITY_SHADOW_ENERGY)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENTITY_SHADOW_ENERGY] is None

    from custom_components.nem_flex_telemetry.coordinator import entry_config

    assert entry_config(entry)[CONF_ENTITY_SHADOW_ENERGY] is None


async def test_options_required_field_missing(hass: HomeAssistant) -> None:
    """With the HAEO source, omitting a required field shows a form error.

    Since #27 the fields are Optional in the form (so the source can be
    switched to Nimbus in one step) and the HAEO requirement is enforced in
    the step handler instead of by the schema.
    """
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = _all_mapped()
    user_input.pop(CONF_ENTITY_TOTAL_LOAD)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_TOTAL_LOAD: "entity_required"}


async def test_options_required_field_blank(hass: HomeAssistant) -> None:
    """A blank required field (if a client sends one) shows entity_required."""
    from custom_components.nem_flex_telemetry.config_flow import (
        NemFlexTelemetryOptionsFlow,
    )

    entry = _entry(hass)
    flow = NemFlexTelemetryOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    user_input = {**_all_mapped(), CONF_ENTITY_TOTAL_LOAD: ""}
    result = await flow.async_step_init(user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_TOTAL_LOAD: "entity_required"}


def test_entry_config_options_override_data() -> None:
    """Coordinator config merges options over data."""
    from types import SimpleNamespace

    from custom_components.nem_flex_telemetry.coordinator import entry_config

    entry = SimpleNamespace(
        data={CONF_ENTITY_TOTAL_LOAD: "sensor.wrong_load", CONF_REGION: "NSW1"},
        options={CONF_ENTITY_TOTAL_LOAD: "sensor.house_load"},
    )
    cfg = entry_config(entry)
    assert cfg[CONF_ENTITY_TOTAL_LOAD] == "sensor.house_load"
    assert cfg[CONF_REGION] == "NSW1"


async def test_update_listener_reloads_entry(hass: HomeAssistant) -> None:
    """Saving options triggers a reload of the entry."""
    from unittest.mock import AsyncMock, patch

    from custom_components.nem_flex_telemetry import async_update_listener

    entry = _entry(hass)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as reload:
        await async_update_listener(hass, entry)
    reload.assert_awaited_once_with(entry.entry_id)


async def test_saving_options_reloads_loaded_entry(hass: HomeAssistant) -> None:
    """End to end: a loaded entry reloads with the new mapping after save."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.nem_flex_telemetry.coordinator import entry_config

    seen_configs: list[dict] = []

    def _fake_coordinator(hass_, entry_):
        seen_configs.append(entry_config(entry_))
        coord = MagicMock()
        coord.async_config_entry_first_refresh = AsyncMock()
        coord.async_load_state = AsyncMock()
        coord.async_handle_stop = AsyncMock()
        coord.async_shutdown = AsyncMock()
        coord.household_id = entry_.data[CONF_HOUSEHOLD_ID]
        coord.region = entry_.data[CONF_REGION]
        return coord

    entry = _entry(hass)
    with (
        patch(
            "custom_components.nem_flex_telemetry.NemFlexTelemetryCoordinator",
            side_effect=_fake_coordinator,
        ),
        patch("custom_components.nem_flex_telemetry.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={**_all_mapped(), CONF_ENTITY_TOTAL_LOAD: "sensor.house_load"},
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(seen_configs) == 2
    assert seen_configs[0][CONF_ENTITY_TOTAL_LOAD] == "sensor.wrong_load"
    assert seen_configs[1][CONF_ENTITY_TOTAL_LOAD] == "sensor.house_load"
