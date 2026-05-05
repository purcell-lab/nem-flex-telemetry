"""Sensor platform for NEM Flex Telemetry.

Exposes four status sensors:
- last_push_time: timestamp of the most recent successful GitHub push
- records_pushed_today: count of records pushed since UTC midnight
- push_errors: cumulative push error count
- cohort_size: number of households in the central repo (best-effort)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HOUSEHOLD_ID,
    DOMAIN,
    SENSOR_COHORT_SIZE,
    SENSOR_LAST_PUSH_TIME,
    SENSOR_PUSH_ERRORS,
    SENSOR_RECORDS_PUSHED_TODAY,
)
from .coordinator import NemFlexTelemetryCoordinator

_LOGGER = logging.getLogger(__name__)

SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    SensorEntityDescription(
        key=SENSOR_LAST_PUSH_TIME,
        name="Last Push Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:cloud-upload",
    ),
    SensorEntityDescription(
        key=SENSOR_RECORDS_PUSHED_TODAY,
        name="Records Pushed Today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="records",
        icon="mdi:database-arrow-up",
    ),
    SensorEntityDescription(
        key=SENSOR_PUSH_ERRORS,
        name="Push Errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="errors",
        icon="mdi:alert-circle",
    ),
    SensorEntityDescription(
        key=SENSOR_COHORT_SIZE,
        name="Cohort Size",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="households",
        icon="mdi:home-group",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NEM Flex Telemetry sensors from a config entry."""
    coordinator: NemFlexTelemetryCoordinator = hass.data[DOMAIN][entry.entry_id]
    household_id = entry.data[CONF_HOUSEHOLD_ID]

    entities = [
        NemFlexTelemetrySensor(coordinator, description, household_id)
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class NemFlexTelemetrySensor(CoordinatorEntity[NemFlexTelemetryCoordinator], SensorEntity):
    """A status sensor for the NEM Flex Telemetry integration."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NemFlexTelemetryCoordinator,
        description: SensorEntityDescription,
        household_id: str,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{household_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, household_id)},
            "name": f"NEM Flex Telemetry ({household_id})",
            "manufacturer": "NEM Flex Telemetry",
            "model": "Demand Flexibility Telemetry",
            "sw_version": "0.1.0",
            "configuration_url": "https://github.com/purcell-lab/nem-flex-telemetry",
        }

    @property
    def native_value(self) -> Any:
        """Return the sensor value from the coordinator data."""
        data = self.coordinator.data
        if data is None:
            return None

        key = self.entity_description.key
        if key == SENSOR_LAST_PUSH_TIME:
            return data.last_push_time  # datetime or None
        if key == SENSOR_RECORDS_PUSHED_TODAY:
            return data.records_pushed_today
        if key == SENSOR_PUSH_ERRORS:
            return data.push_errors
        if key == SENSOR_COHORT_SIZE:
            return data.cohort_size
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "household_id": self.coordinator.household_id,
            "region": self.coordinator.region,
            "buffer_size": self.coordinator.data.buffer_size if self.coordinator.data else 0,
        }
