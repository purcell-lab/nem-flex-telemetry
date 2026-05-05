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

from .const import DOMAIN, PLATFORMS, SERVICE_MANUAL_PUSH, VERSION
from .coordinator import NemFlexTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the NEM Flex Telemetry component (YAML config not supported)."""
    hass.data.setdefault(DOMAIN, {})
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
