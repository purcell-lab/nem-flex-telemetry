"""DataUpdateCoordinator for NEM Flex Telemetry.

Responsibilities:
- Read HAEO entity states every 5 minutes
- Build and validate the 12-field telemetry record
- Buffer records in memory
- Push the buffer to GitHub on the hour (every 12 records = 1 hour of data)
- Expose status attributes to sensor.py

All blocking I/O (PyGithub, voluptuous) is run via async_add_executor_job.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ENTITY_BASELINE,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SETPOINT,
    CONF_ENTITY_SOC,
    CONF_GITHUB_PAT,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    DOMAIN,
    GITHUB_REPO,
    RECORDS_PER_PUSH,
    UPDATE_INTERVAL_SECONDS,
)
from .github_client import GitHubPushError, NemFlexGitHubClient

_LOGGER = logging.getLogger(__name__)

# Voluptuous schema for a single telemetry record (integration-side validation)
_NEM_REGIONS = vol.In(["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])

RECORD_SCHEMA = vol.Schema(
    {
        vol.Required("interval_start_utc"): str,
        vol.Required("region"): _NEM_REGIONS,
        vol.Required("postcode_prefix"): vol.Match(r"^[0-9]{3}$"),
        vol.Required("net_import_kw"): vol.Coerce(float),
        vol.Required("price_signal_seen"): vol.Coerce(float),
        vol.Required("optimiser_setpoint_kw"): vol.Coerce(float),
        vol.Required("flex_available_up_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("flex_available_down_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("storage_soc_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("envelope_import_limit_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("envelope_export_limit_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("naive_baseline_kw"): vol.Coerce(float),
    }
)


def _read_state_float(hass: HomeAssistant, entity_id: str, fallback: float = 0.0) -> float:
    """Read a HA entity state as a float.

    Returns the fallback value if the entity is unavailable or unparseable.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        _LOGGER.debug("Entity %s is unavailable, using fallback %s", entity_id, fallback)
        return fallback
    try:
        return float(state.state)
    except ValueError:
        _LOGGER.warning("Could not parse state '%s' from entity %s as float", state.state, entity_id)
        return fallback


class CoordinatorData:
    """Data class holding coordinator output for sensor consumption."""

    def __init__(self) -> None:
        """Initialise with default values."""
        self.last_push_time: datetime | None = None
        self.records_pushed_today: int = 0
        self.push_errors: int = 0
        self.cohort_size: int = 0
        self.buffer_size: int = 0


class NemFlexTelemetryCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Coordinate 5-minute telemetry reads and hourly GitHub pushes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._entry = entry
        self._config = entry.data

        self.household_id: str = self._config[CONF_HOUSEHOLD_ID]
        self.region: str = self._config[CONF_REGION]
        self._postcode_prefix: str = self._config[CONF_POSTCODE_PREFIX]

        # In-memory record buffer (max 24 hours of data = 288 records)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=288)
        self._data = CoordinatorData()

        # Lazy-initialised GitHub client (created in executor)
        self._github_client: NemFlexGitHubClient | None = None
        self._push_error_count: int = 0
        self._records_pushed_today: int = 0
        self._last_push_day: int | None = None

    def _get_or_create_github_client(self) -> NemFlexGitHubClient:
        """Return the GitHub client, creating it if needed (blocking, run in executor)."""
        if self._github_client is None:
            self._github_client = NemFlexGitHubClient(
                pat=self._config[CONF_GITHUB_PAT],
                repo_name=GITHUB_REPO,
            )
        return self._github_client

    def _build_record(self) -> dict[str, Any]:
        """Read all HAEO entity states and build a telemetry record.

        This runs in the main HA event loop (entity state reads are non-blocking).
        """
        now_utc = datetime.now(tz=UTC)
        # Round down to the nearest 5-minute boundary
        minutes = (now_utc.minute // 5) * 5
        interval_start = now_utc.replace(minute=minutes, second=0, microsecond=0)

        record: dict[str, Any] = {
            "interval_start_utc": interval_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": self.region,
            "postcode_prefix": self._postcode_prefix,
            "net_import_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_NET_IMPORT]),
            "price_signal_seen": _read_state_float(self.hass, self._config[CONF_ENTITY_PRICE_SIGNAL]),
            "optimiser_setpoint_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_SETPOINT]),
            "flex_available_up_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_FLEX_UP]),
            "flex_available_down_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_FLEX_DOWN]),
            "storage_soc_pct": _read_state_float(self.hass, self._config[CONF_ENTITY_SOC]),
            "envelope_import_limit_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_ENVELOPE_IMPORT], fallback=5.0),
            "envelope_export_limit_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_ENVELOPE_EXPORT], fallback=5.0),
            "naive_baseline_kw": _read_state_float(self.hass, self._config[CONF_ENTITY_BASELINE]),
        }
        return record

    def _validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run voluptuous validation. Raises vol.Invalid on failure (runs in executor)."""
        return RECORD_SCHEMA(record)

    async def _async_update_data(self) -> CoordinatorData:
        """Poll HAEO entities, buffer the record, and push to GitHub when due.

        Called automatically every UPDATE_INTERVAL_SECONDS by the base class.
        """
        # Build record in the event loop (state reads are safe here)
        record = self._build_record()

        # Validate in executor (voluptuous is synchronous but cheap)
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, record
            )
        except vol.Invalid as exc:
            _LOGGER.error("Telemetry record validation failed: %s. Record: %s", exc, record)
            self._data.push_errors = self._push_error_count + 1
            raise UpdateFailed(f"Record validation failed: {exc}") from exc

        self._buffer.append(validated)
        _LOGGER.debug("Buffered record %s (buffer size: %d)", validated["interval_start_utc"], len(self._buffer))

        # Push when buffer reaches RECORDS_PER_PUSH (1 hour of data)
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()

        # Reset daily counter at midnight UTC
        today = datetime.now(tz=UTC).day
        if self._last_push_day is not None and self._last_push_day != today:
            self._records_pushed_today = 0
        self._last_push_day = today

        # Update status data
        self._data.buffer_size = len(self._buffer)
        self._data.records_pushed_today = self._records_pushed_today
        self._data.push_errors = self._push_error_count

        # Fetch cohort size occasionally (every ~6 hours = 72 intervals)
        # Use buffer modulus as a cheap approximation
        total_records = self._records_pushed_today
        if total_records % 72 == 0:
            try:
                cohort_size = await self.hass.async_add_executor_job(
                    lambda: self._get_or_create_github_client().get_cohort_size()
                )
                self._data.cohort_size = cohort_size
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.debug("Could not update cohort size: %s", exc)

        return self._data

    async def _async_push_buffer(self) -> None:
        """Flush the in-memory buffer to GitHub."""
        if not self._buffer:
            return

        records_to_push = list(self._buffer)
        self._buffer.clear()

        try:
            await self.hass.async_add_executor_job(
                lambda: self._get_or_create_github_client().append_records(
                    self.household_id, records_to_push
                )
            )
            count = len(records_to_push)
            self._records_pushed_today += count
            self._data.last_push_time = datetime.now(tz=UTC)
            _LOGGER.info("Pushed %d records for household %s", count, self.household_id)

        except GitHubPushError as exc:
            self._push_error_count += 1
            # Re-add records to front of buffer so they are retried next cycle
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            _LOGGER.error("GitHub push failed (total errors: %d): %s", self._push_error_count, exc)

    async def async_force_push(self) -> None:
        """Force an immediate push of the current buffer (for manual push service)."""
        _LOGGER.info("Force push triggered for household %s", self.household_id)
        await self._async_push_buffer()

    async def async_shutdown(self) -> None:
        """Attempt a final flush before unloading."""
        _LOGGER.info("Coordinator shutting down, attempting final push for %s", self.household_id)
        await self._async_push_buffer()
