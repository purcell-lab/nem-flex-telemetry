"""NEM Flex Telemetry integration for Home Assistant.

Reads HAEO demand-flexibility entities and pushes 5-minute interval
telemetry records to the central NEM Flex Telemetry GitHub repository.

Authentication: GitHub OAuth Device Flow (v0.2.0+). No PATs to manage.
Schema: assets[]-based telemetry record (schema v2.0, $/kWh, shadow prices).

Licence: MIT (code), CC-BY-4.0 (data contributed to the repo).
Repo: https://github.com/purcell-lab/nem-flex-telemetry
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    CONF_ENTITY_SHADOW_ENERGY,
    DEFAULT_HAEO_ENTITIES,
    DOMAIN,
    PLATFORMS,
    SERVICE_MANUAL_PUSH,
    VERSION,
)
from .coordinator import NemFlexTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the NEM Flex Telemetry component (YAML config not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry forward when the schema version bumps.

    v2 -> v3:
        Inject the new ``CONF_ENTITY_SHADOW_ENERGY`` key, defaulting it to
        the discovery primary (``sensor.switchboard_power_balance_shadow_price``).
        The old install pre-dates this field so existing entries have no
        value for it; this migration backfills it without forcing the user
        to delete and re-add the integration.
    """
    _LOGGER.info(
        "Migrating NEM Flex Telemetry config entry %s from v%s",
        entry.entry_id,
        entry.version,
    )

    if entry.version < 3:
        new_data = {**entry.data}
        if CONF_ENTITY_SHADOW_ENERGY not in new_data:
            spec = DEFAULT_HAEO_ENTITIES.get(CONF_ENTITY_SHADOW_ENERGY, {})
            primary = spec.get("primary")
            if primary:
                new_data[CONF_ENTITY_SHADOW_ENERGY] = primary
                _LOGGER.info(
                    "v2->v3: set %s = %s for entry %s",
                    CONF_ENTITY_SHADOW_ENERGY,
                    primary,
                    entry.entry_id,
                )
        hass.config_entries.async_update_entry(entry, data=new_data, version=3)

    _LOGGER.info(
        "NEM Flex Telemetry config entry %s migrated to v%s",
        entry.entry_id,
        entry.version,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NEM Flex Telemetry from a config entry.

    Creates the DataUpdateCoordinator and forwards setup to sensor platform.
    Logs version and schema version at startup.
    """
    hass.data.setdefault(DOMAIN, {})

    coordinator = NemFlexTelemetryCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register platforms (sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the manual push service (idempotent: only once per domain).
    # This is an integration-wide action with no target, so we use a plain
    # voluptuous schema rather than ``cv.make_entity_service_schema``, which
    # would force callers to supply entity_id / device_id / area_id / floor_id
    # / label_id and reject calls made from Developer Tools without a target.
    if not hass.services.has_service(DOMAIN, SERVICE_MANUAL_PUSH):
        async def handle_manual_push(call: ServiceCall) -> None:
            """Handle a manual push service call (useful for testing)."""
            _LOGGER.info("Manual push triggered for entry %s", entry.entry_id)
            await coordinator.async_force_push()

        hass.services.async_register(
            DOMAIN,
            SERVICE_MANUAL_PUSH,
            handle_manual_push,
            schema=vol.Schema({}),
        )

    _LOGGER.info(
        "NEM Flex Telemetry v%s set up for household '%s' in region %s (schema v2.0)",
        VERSION,
        coordinator.household_id,
        coordinator.region,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a NEM Flex Telemetry config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: NemFlexTelemetryCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    # Remove service only when the last entry is unloaded
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_MANUAL_PUSH)

    return unload_ok
