"""DataUpdateCoordinator for NEM Flex Telemetry.

Responsibilities:
- Read HAEO entity states every 5 minutes
- Build and validate the 13-field telemetry record (schema v1.1)
- Derive flex headroom from battery limits when HAEO does not expose them directly
- Buffer records in memory
- Push the buffer to GitHub on the hour (every 12 records = 1 hour of data)
- Expose status attributes to sensor.py
- Trigger HA re-authentication when the stored OAuth token is rejected (401)
- Discover and log context entities (PD7day forecast, EV SOC) for v0.3

All GitHub I/O is async (aiohttp via NemFlexGitHubClient).
Version: 0.2.0 / Schema: 1.1
"""

from __future__ import annotations

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
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SETPOINT,
    CONF_ENTITY_SOC,
    CONF_GITHUB_LOGIN,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_BATTERY_MAX_CHARGE_KW,
    DEFAULT_BATTERY_MAX_DISCHARGE_KW,
    DOMAIN,
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    GITHUB_REPO,
    RECORDS_PER_PUSH,
    UPDATE_INTERVAL_SECONDS,
    VERSION,
)
from .discovery import discover_context_entities
from .github_client import GitHubPushError, NemFlexGitHubClient, TokenInvalidError

_LOGGER = logging.getLogger(__name__)

# Price unit conversion: entities report $/kWh, schema stores $/MWh
_KWH_TO_MWH_MULTIPLIER = 1000.0

# Voluptuous schema for a single telemetry record (schema v1.1)
_NEM_REGIONS = vol.In(["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])

RECORD_SCHEMA = vol.Schema(
    {
        vol.Required("interval_start_utc"): str,
        vol.Required("region"): _NEM_REGIONS,
        vol.Required("postcode_prefix"): vol.Match(r"^[0-9]{3}$"),
        vol.Required("net_import_kw"): vol.Coerce(float),
        vol.Required("price_signal_seen"): vol.Coerce(float),
        vol.Required("price_export_seen"): vol.Coerce(float),
        vol.Required("optimiser_setpoint_kw"): vol.Coerce(float),
        vol.Required("flex_available_up_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("flex_available_down_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("storage_soc_pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Required("envelope_import_limit_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("envelope_export_limit_kw"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required("naive_baseline_kw"): vol.Coerce(float),
    }
)


def _read_state_float(
    hass: HomeAssistant, entity_id: str | None, fallback: float = 0.0
) -> float:
    """Read a HA entity state as a float.

    Returns the fallback if entity_id is None, entity is absent, or state
    is unavailable/unknown/unparseable.
    """
    if not entity_id:
        return fallback
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        _LOGGER.debug(
            "Entity %s is unavailable, using fallback %s", entity_id, fallback
        )
        return fallback
    try:
        return float(state.state)
    except ValueError:
        _LOGGER.warning(
            "Could not parse state '%s' from entity %s as float",
            state.state,
            entity_id,
        )
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
        self.config_entry = entry
        self._config = entry.data

        self.household_id: str = self._config[CONF_HOUSEHOLD_ID]
        self.region: str = self._config[CONF_REGION]
        self._postcode_prefix: str = self._config[CONF_POSTCODE_PREFIX]
        self._github_login: str = self._config.get(CONF_GITHUB_LOGIN, "")

        # In-memory record buffer (max 24 hours = 288 records)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=288)
        self._data = CoordinatorData()

        # Lazy-initialised GitHub client
        self._github_client: NemFlexGitHubClient | None = None
        self._push_error_count: int = 0
        self._records_pushed_today: int = 0
        self._last_push_day: int | None = None

        # Track whether flex headroom is derived (for one-time startup log)
        self._flex_derived_logged: bool = False

        # Context entities discovered at first update (v0.3 forecast horizon)
        self._context_entities: dict[str, str | None] = {}
        self._context_discovered: bool = False

    def _get_or_create_github_client(self) -> NemFlexGitHubClient:
        """Return the GitHub client, creating it if needed."""
        if self._github_client is None:
            self._github_client = NemFlexGitHubClient(
                token=self._config[CONF_TOKEN],
                repo_name=GITHUB_REPO,
            )
        return self._github_client

    def _read_battery_active_power(self) -> float:
        """Read the battery active power (setpoint entity). Positive = charging."""
        return _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_SETPOINT), fallback=0.0
        )

    def _read_battery_max_charge(self) -> float:
        """Read battery max charge rate, falling back to the configured default."""
        state = self.hass.states.get(ENTITY_BATTERY_MAX_CHARGE)
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                return float(state.state)
            except ValueError:
                pass
        return DEFAULT_BATTERY_MAX_CHARGE_KW

    def _read_battery_max_discharge(self) -> float:
        """Read battery max discharge rate, falling back to the configured default."""
        state = self.hass.states.get(ENTITY_BATTERY_MAX_DISCHARGE)
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                return float(state.state)
            except ValueError:
                pass
        return DEFAULT_BATTERY_MAX_DISCHARGE_KW

    def _derive_flex_headroom(
        self,
    ) -> tuple[float, float]:
        """Derive flex_available_up/down from battery limits.

        Formula:
            flex_up  = max_charge  - max(0, battery_active_power)
                       # extra room to absorb: full charge capacity minus current charge rate
            flex_down = max_discharge - max(0, -battery_active_power)
                       # extra room to discharge: full discharge capacity minus current discharge rate

        Both are clamped to >= 0.

        Returns: (flex_up_kw, flex_down_kw)
        """
        if not self._flex_derived_logged:
            _LOGGER.info(
                "flex_available_up/down derived from battery limits since HAEO does not "
                "expose them directly. Add sensor.haeo_flex_up / sensor.haeo_flex_down "
                "to your instance for higher fidelity."
            )
            self._flex_derived_logged = True

        bap = self._read_battery_active_power()
        max_charge = self._read_battery_max_charge()
        max_discharge = self._read_battery_max_discharge()

        # Positive bap = charging; negative = discharging
        current_charge_rate = max(0.0, bap)
        current_discharge_rate = max(0.0, -bap)

        flex_up = max(0.0, max_charge - current_charge_rate)
        flex_down = max(0.0, max_discharge - current_discharge_rate)
        return flex_up, flex_down

    def _build_record(self) -> dict[str, Any]:
        """Read all HAEO entity states and build a 13-field telemetry record.

        Runs in the main HA event loop (state reads are non-blocking).
        Price values stored in $/kWh by HAEO entities are converted to $/MWh.
        Flex headroom is derived from battery limits if not exposed by HAEO.
        """
        now_utc = datetime.now(tz=UTC)
        minutes = (now_utc.minute // 5) * 5
        interval_start = now_utc.replace(minute=minutes, second=0, microsecond=0)

        # Flex headroom: use entity if available, otherwise derive
        flex_up_entity = self._config.get(CONF_ENTITY_FLEX_UP)
        flex_down_entity = self._config.get(CONF_ENTITY_FLEX_DOWN)

        if flex_up_entity and self.hass.states.get(flex_up_entity) is not None:
            flex_up = _read_state_float(self.hass, flex_up_entity, fallback=0.0)
        else:
            flex_up, _ = self._derive_flex_headroom()

        if flex_down_entity and self.hass.states.get(flex_down_entity) is not None:
            flex_down = _read_state_float(self.hass, flex_down_entity, fallback=0.0)
        else:
            _, flex_down = self._derive_flex_headroom()

        # Price conversion: $/kWh -> $/MWh
        price_buy_kwh = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_PRICE_SIGNAL), fallback=0.0
        )
        price_export_kwh = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_PRICE_EXPORT), fallback=0.0
        )

        record: dict[str, Any] = {
            "interval_start_utc": interval_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": self.region,
            "postcode_prefix": self._postcode_prefix,
            "net_import_kw": _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_NET_IMPORT), fallback=0.0
            ),
            "price_signal_seen": round(price_buy_kwh * _KWH_TO_MWH_MULTIPLIER, 4),
            "price_export_seen": round(price_export_kwh * _KWH_TO_MWH_MULTIPLIER, 4),
            "optimiser_setpoint_kw": _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_SETPOINT), fallback=0.0
            ),
            "flex_available_up_kw": max(0.0, flex_up),
            "flex_available_down_kw": max(0.0, flex_down),
            "storage_soc_pct": _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_SOC), fallback=0.0
            ),
            "envelope_import_limit_kw": _read_state_float(
                self.hass,
                self._config.get(CONF_ENTITY_ENVELOPE_IMPORT),
                fallback=5.0,
            ),
            "envelope_export_limit_kw": abs(
                _read_state_float(
                    self.hass,
                    self._config.get(CONF_ENTITY_ENVELOPE_EXPORT),
                    fallback=5.0,
                )
            ),
            "naive_baseline_kw": _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_BASELINE), fallback=0.0
            ),
        }
        return record

    def _validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run voluptuous validation. Raises vol.Invalid on failure."""
        return RECORD_SCHEMA(record)

    async def _async_discover_context(self) -> None:
        """Discover context entities at first update and log them."""
        self._context_entities = await discover_context_entities(
            self.hass, region=self.region
        )
        self._context_discovered = True
        _LOGGER.info(
            "Context entities for v0.3 forecast horizon: %s",
            {k: v for k, v in self._context_entities.items() if v is not None},
        )

    async def _async_update_data(self) -> CoordinatorData:
        """Poll HAEO entities, buffer the record, and push to GitHub when due.

        Called automatically every UPDATE_INTERVAL_SECONDS by the base class.
        Triggers re-authentication if the stored token is rejected by GitHub.
        """
        # One-time context entity discovery
        if not self._context_discovered:
            await self._async_discover_context()

        record = self._build_record()

        # Validate (voluptuous is synchronous; run in executor)
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, record
            )
        except vol.Invalid as exc:
            _LOGGER.error(
                "Telemetry record validation failed: %s. Record: %s", exc, record
            )
            self._data.push_errors = self._push_error_count + 1
            raise UpdateFailed(f"Record validation failed: {exc}") from exc

        self._buffer.append(validated)
        _LOGGER.debug(
            "Buffered record %s (buffer size: %d)",
            validated["interval_start_utc"],
            len(self._buffer),
        )

        # Push when buffer reaches RECORDS_PER_PUSH (1 hour of data)
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()

        # Reset daily counter at midnight UTC
        today = datetime.now(tz=UTC).day
        if self._last_push_day is not None and self._last_push_day != today:
            self._records_pushed_today = 0
        self._last_push_day = today

        self._data.buffer_size = len(self._buffer)
        self._data.records_pushed_today = self._records_pushed_today
        self._data.push_errors = self._push_error_count

        # Fetch cohort size occasionally (~every 6 hours = 72 intervals)
        if self._records_pushed_today % 72 == 0:
            try:
                cohort_size = await self._get_or_create_github_client().get_cohort_size()
                self._data.cohort_size = cohort_size
            except TokenInvalidError as exc:
                _LOGGER.warning(
                    "Token invalid while fetching cohort size: %s. Triggering reauth.", exc
                )
                self.config_entry.async_start_reauth(self.hass)
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
            await self._get_or_create_github_client().append_records(
                self.household_id, records_to_push
            )
            count = len(records_to_push)
            self._records_pushed_today += count
            self._data.last_push_time = datetime.now(tz=UTC)
            _LOGGER.info(
                "Pushed %d records for household %s (schema v1.1, v%s)",
                count,
                self.household_id,
                VERSION,
            )

        except TokenInvalidError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            _LOGGER.error(
                "GitHub token invalid (total errors: %d): %s. Triggering re-authentication.",
                self._push_error_count,
                exc,
            )
            self.config_entry.async_start_reauth(self.hass)

        except GitHubPushError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            _LOGGER.error(
                "GitHub push failed (total errors: %d): %s",
                self._push_error_count,
                exc,
            )

    async def async_force_push(self) -> None:
        """Force an immediate push of the current buffer (for manual push service)."""
        _LOGGER.info("Force push triggered for household %s", self.household_id)
        await self._async_push_buffer()

    async def async_shutdown(self) -> None:
        """Attempt a final flush before unloading."""
        _LOGGER.info(
            "Coordinator shutting down, attempting final push for %s",
            self.household_id,
        )
        await self._async_push_buffer()
