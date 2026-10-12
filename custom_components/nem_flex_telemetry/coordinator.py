"""DataUpdateCoordinator for NEM Flex Telemetry.

Responsibilities:
- Read HAEO entity states every 5 minutes
- Build and validate the schema v2.0 telemetry record (18 flat fields + assets[] + deferrable_loads[])
- Derive flex headroom from battery limits when HAEO does not expose them directly
- Infer per-EV connection state and power_flow_capability (not from any entity)
- Build asset records for the configured batteries and EVs only (#15)
- Allocate the configured bidirectional chargers to EVs (sticky)
- Re-run global entity sweep on every coordinator startup
- Buffer records in memory
- Push the buffer to GitHub on the hour (every 12 records = 1 hour of data)
- Expose status attributes to sensor.py
- Trigger HA re-authentication when the stored OAuth token is rejected (401)

All prices are stored in $/kWh (no /1000 conversion from v2.0 onwards).
All GitHub I/O is async (aiohttp via NemFlexGitHubClient).
Version: 0.3.0 / Schema: 2.0
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .assets import configured_assets
from .const import (
    ASSET_KIND_BATTERY,
    ASSET_KIND_EV,
    SHADOW_PRICE_MAX,
    SHADOW_PRICE_MIN,
    CONF_BIDIRECTIONAL_CHARGERS,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SHADOW_ENERGY,
    CONF_ENTITY_SHADOW_ENVELOPE_EXPORT,
    CONF_ENTITY_SHADOW_ENVELOPE_IMPORT,
    CONF_ENTITY_SHADOW_LOAD_FORECAST,
    CONF_ENTITY_SHADOW_SOLAR_FORECAST,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_GITHUB_LOGIN,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_BATTERY_MAX_CHARGE_KW,
    DEFAULT_BATTERY_MAX_DISCHARGE_KW,
    DEFAULT_BIDIRECTIONAL_CHARGERS,
    DEFAULT_DCEV_AC_TO_DC_KW,
    DEFAULT_DCEV_DC_TO_AC_KW,
    DEFAULT_EV_MAX_CHARGE_KW,
    DEFAULT_EV_MAX_DISCHARGE_KW,
    DEFAULT_INVERTER_AC_TO_DC_KW,
    DEFAULT_INVERTER_DC_TO_AC_KW,
    DOMAIN,
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_DCEV_AC_TO_DC,
    ENTITY_DCEV_DC_TO_AC,
    ENTITY_INVERTER_AC_TO_DC,
    ENTITY_INVERTER_DC_TO_AC,
    GITHUB_REPO,
    NIMBUS_TELEMETRY_ENTITY,
    RECORDS_PER_PUSH,
    REQUIRED_ENTITY_FIELDS,
    SOURCE_HAEO,
    SOURCE_NIMBUS,
    CONF_SOURCE,
    SCHEMA_VERSION,
    UPDATE_INTERVAL_SECONDS,
    VERSION,
)
from .discovery import discover_context_entities, run_global_sweep
from .nimbus_source import read_nimbus_record
from homeassistant.helpers import issue_registry as ir

from .github_client import (
    GitHubPushError,
    NemFlexGitHubClient,
    PushPermissionError,
    TokenInvalidError,
)

ISSUE_NO_PUSH_ACCESS = "no_push_access"

_LOGGER = logging.getLogger(__name__)

# Persistent state (#18, #19): buffer and push stats survive HA restarts.
STORAGE_VERSION = 1
BUFFER_MAX_RECORDS = 288                 # 24 hours of 5-minute records
COHORT_REFRESH = timedelta(hours=6)
STOP_PUSH_TIMEOUT_S = 10.0               # best-effort push on HA stop

# Startup readiness (#21). HAEO and inverter integrations can take a few
# minutes to publish states after a restart. During this grace period
# fallbacks and missing inputs are logged at DEBUG only.
STARTUP_GRACE_INTERVALS = 3

# EV connection state inference constants
_SOC_DELTA_PLUGGED_IDLE_MAX = 0.5     # % per interval; below this = plugged_idle
_SOC_DELTA_CHARGE_MIN = 1.0           # % per 5min; rising at this rate = charging
_SOC_DELTA_DISCHARGE_MIN = 1.0        # % per 5min; falling at this rate = discharging
_SOC_DELTA_DRIVING_MIN = 1.5          # % per 5min; rapid drop with no shadow = driving
_BIDIRECTIONAL_STICKY_HOURS = 1       # hours; once seen discharging, stays bidirectional for this long


def _read_state_float(
    hass: HomeAssistant, entity_id: str | None, fallback: float | None = 0.0
) -> float | None:
    """Read a HA entity state as a float.

    Returns fallback if entity_id is None, entity is absent, or state
    is unavailable/unknown/unparseable.
    """
    if not entity_id:
        return fallback
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        _LOGGER.debug("Entity %s is unavailable, using fallback %s", entity_id, fallback)
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


def _read_state_float_or_none(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a HA entity state as a float, returning None if unavailable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", ""):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


class CoordinatorData:
    """Data class holding coordinator output for sensor consumption."""

    def __init__(self) -> None:
        """Initialise with default values."""
        self.last_push_time: datetime | None = None
        self.records_pushed_today: int = 0
        self.push_errors: int = 0
        self.cohort_size: int = 0
        self.buffer_size: int = 0
        self.skipped_intervals: int = 0
        self.validation_errors: int = 0
        self.source: str = SOURCE_HAEO
        self.source_status: str | None = None
        self.unmapped_entities: list[str] = []


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Return the effective configuration for an entry.

    Setup values live in ``entry.data``; later edits from the options flow
    live in ``entry.options`` and take precedence (#16).
    """
    return {**entry.data, **entry.options}


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
        self._config = entry_config(entry)

        self.household_id: str = self._config[CONF_HOUSEHOLD_ID]
        self.region: str = self._config[CONF_REGION]
        self._postcode_prefix: str = self._config[CONF_POSTCODE_PREFIX]
        self._github_login: str = self._config.get(CONF_GITHUB_LOGIN, "")

        # Record source (#27): build from mapped entities, or relay Nimbus.
        self.source: str = self._config.get(CONF_SOURCE) or SOURCE_HAEO
        self._nimbus_unsub: CALLBACK_TYPE | None = None

        # Record buffer (max 24 hours = 288 records). Persisted to
        # .storage/nem_flex_telemetry.<entry_id> so a restart does not lose
        # records waiting for the hourly push (#18).
        self._buffer: deque[dict[str, Any]] = deque(maxlen=BUFFER_MAX_RECORDS)
        self._data = CoordinatorData()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

        # Lazy-initialised GitHub client
        self._github_client: NemFlexGitHubClient | None = None
        self._push_error_count: int = 0
        self._no_push_access: bool = False
        self._records_pushed_today: int = 0
        # Local (HA timezone) date the daily counter belongs to (#19).
        self._push_day: str | None = None
        self._cohort_checked: datetime | None = None

        # Flex derivation logging gate
        self._flex_derived_logged: bool = False

        # Power-rating health-check (#21): last known live/fallback status per
        # entity, so changes are logged and startup fallbacks are not reported
        # as permanent.
        self._power_rating_live: dict[str, bool] = {}
        self._power_ratings_logged: bool = False
        self._intervals_seen: int = 0
        self._skipped_intervals: int = 0
        self._missing_warned: frozenset[str] = frozenset()
        # Records rejected by _validate_record (#30): counted and persisted
        # separately from push errors; one WARNING per failing field.
        self._validation_error_count: int = 0
        self._invalid_warned: set[str] = set()

        # Context entities discovered at first update
        self._context_entities: dict[str, str | None] = {}
        self._context_discovered: bool = False

        # Global sweep: re-run on every coordinator startup
        self._last_sweep_unmapped: list[str] = []

        # Per-EV SOC history for connection state inference
        # {asset_id: (prev_soc, prev_timestamp)}
        self._ev_prev_soc: dict[str, tuple[float, datetime]] = {}

        # Bidirectional charger sticky tracking
        # {asset_id: last_discharge_utc}
        self._last_discharge_time: dict[str, datetime] = {}

        # Configured batteries and EVs (#15). Entries from before #15 get the
        # reference home_battery / ev1 / ev2 trio (see assets.legacy_assets).
        self._assets: list[dict[str, Any]] = configured_assets(self._config)
        # Assets skipped because their SOC entity does not exist, so the
        # warning is logged once rather than every interval.
        self._missing_assets: set[str] = set()

        # Bidirectional (V2G) chargers shared by the EVs marked
        # bidirectional_capable, and which EVs currently hold one (sticky,
        # oldest first). The reference install has one shared DCEV charger.
        self._bidirectional_chargers: int = int(
            self._config.get(CONF_BIDIRECTIONAL_CHARGERS, DEFAULT_BIDIRECTIONAL_CHARGERS)
        )
        self._bidirectional_holders: list[str] = []

    def _get_or_create_github_client(self) -> NemFlexGitHubClient:
        """Return the GitHub client, creating it if needed."""
        if self._github_client is None:
            self._github_client = NemFlexGitHubClient(
                token=self._config[CONF_TOKEN],
                repo_name=GITHUB_REPO,
            )
        return self._github_client

    def _read_number_kw(self, entity_id: str, default_kw: float) -> float:
        """Read a number.* power-rating entity in kW, with default fallback.

        Used for battery / inverter / DCEV charger power ratings.
        """
        state = self.hass.states.get(entity_id)
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                return abs(float(state.state))
            except ValueError:
                pass
        return default_kw

    def _read_battery_max_charge(self) -> float:
        return self._read_number_kw(
            ENTITY_BATTERY_MAX_CHARGE, DEFAULT_BATTERY_MAX_CHARGE_KW
        )

    def _read_battery_max_discharge(self) -> float:
        return self._read_number_kw(
            ENTITY_BATTERY_MAX_DISCHARGE, DEFAULT_BATTERY_MAX_DISCHARGE_KW
        )

    def _read_inverter_ac_to_dc(self) -> float:
        return self._read_number_kw(
            ENTITY_INVERTER_AC_TO_DC, DEFAULT_INVERTER_AC_TO_DC_KW
        )

    def _read_inverter_dc_to_ac(self) -> float:
        return self._read_number_kw(
            ENTITY_INVERTER_DC_TO_AC, DEFAULT_INVERTER_DC_TO_AC_KW
        )

    def _read_dcev_ac_to_dc(self) -> float:
        return self._read_number_kw(ENTITY_DCEV_AC_TO_DC, DEFAULT_DCEV_AC_TO_DC_KW)

    def _read_dcev_dc_to_ac(self) -> float:
        return self._read_number_kw(ENTITY_DCEV_DC_TO_AC, DEFAULT_DCEV_DC_TO_AC_KW)

    def _resolve_number_kw(
        self, entity_id: str, default_kw: float
    ) -> tuple[float, bool]:
        """Read a number.* entity, returning (value_kw, is_live).

        is_live=True means the entity exists and parsed cleanly; is_live=False
        means we fell back to the compile-time default. Used by the startup
        health-check log so the user can see at a glance which power-rating
        sensors resolved live vs fell back to constants.
        """
        state = self.hass.states.get(entity_id)
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                return abs(float(state.state)), True
            except ValueError:
                pass
        return default_kw, False

    def _log_power_rating_health_check(self) -> None:
        """Log which power-rating entities are live and which use fallbacks.

        Runs every interval but only logs changes. During the startup grace
        period a fallback is logged at DEBUG, because the entity may simply
        not have published yet (#21). After the grace period a remaining
        fallback is logged once as a WARNING, and a later recovery is logged
        at INFO.
        """
        checks = (
            ("battery max charge", ENTITY_BATTERY_MAX_CHARGE,
             DEFAULT_BATTERY_MAX_CHARGE_KW),
            ("battery max discharge", ENTITY_BATTERY_MAX_DISCHARGE,
             DEFAULT_BATTERY_MAX_DISCHARGE_KW),
            ("hybrid inverter AC->DC", ENTITY_INVERTER_AC_TO_DC,
             DEFAULT_INVERTER_AC_TO_DC_KW),
            ("hybrid inverter DC->AC", ENTITY_INVERTER_DC_TO_AC,
             DEFAULT_INVERTER_DC_TO_AC_KW),
            ("DCEV charger AC->DC", ENTITY_DCEV_AC_TO_DC,
             DEFAULT_DCEV_AC_TO_DC_KW),
            ("DCEV charger DC->AC", ENTITY_DCEV_DC_TO_AC,
             DEFAULT_DCEV_DC_TO_AC_KW),
        )
        in_grace = self._intervals_seen <= STARTUP_GRACE_INTERVALS
        live_count = 0
        for label, entity_id, default_kw in checks:
            value, is_live = self._resolve_number_kw(entity_id, default_kw)
            live_count += is_live
            previous = self._power_rating_live.get(entity_id)
            if is_live:
                if previous is not True:
                    _LOGGER.info(
                        "Power-rating health-check: %s -> %.1f kW (live from %s)",
                        label, value, entity_id,
                    )
                self._power_rating_live[entity_id] = True
            elif in_grace:
                _LOGGER.debug(
                    "Power-rating health-check: %s not ready yet (%s); "
                    "using %.1f kW until it publishes",
                    label, entity_id, value,
                )
            elif previous is not False:
                _LOGGER.warning(
                    "Power-rating health-check: %s -> %.1f kW "
                    "(FALLBACK; %s missing or unavailable)",
                    label, value, entity_id,
                )
                self._power_rating_live[entity_id] = False
        if not self._power_ratings_logged and (not in_grace or live_count == len(checks)):
            _LOGGER.info(
                "Power-rating health-check complete: %d live, %d fallback",
                live_count, len(checks) - live_count,
            )
            self._power_ratings_logged = True

    def _missing_required_inputs(self) -> list[str]:
        """Return required entity fields whose entity has no usable state."""
        missing: list[str] = []
        for field in REQUIRED_ENTITY_FIELDS:
            entity_id = self._config.get(field)
            if _read_state_float_or_none(self.hass, entity_id) is None:
                missing.append(f"{field}={entity_id or 'not mapped'}")
        return missing

    def _battery_asset_flex(
        self, battery_setpoint_kw: float
    ) -> tuple[float, float]:
        """Per-asset battery flex headroom, raw battery rating only.

        Reports the battery's own DC-side limits as published by
        number.battery_max_charge_power / number.battery_max_discharge_power.
        The hybrid inverter ceiling is applied at the household level in
        _build_record, NOT here, because the inverter is shared with PV and
        DCEV flow paths.

        Returns (available_up_kw, available_down_kw), both >= 0.
        """
        max_charge = self._read_battery_max_charge()
        max_discharge = self._read_battery_max_discharge()
        current_charge_rate = max(0.0, battery_setpoint_kw)
        current_discharge_rate = max(0.0, -battery_setpoint_kw)
        return (
            max(0.0, max_charge - current_charge_rate),
            max(0.0, max_discharge - current_discharge_rate),
        )

    def _ev_asset_flex(
        self,
        asset_id: str,
        ev_setpoint_kw: float,
        connection_state: str,
        power_flow_capability: str,
        bidirectional_capable: bool = True,
    ) -> tuple[float, float]:
        """Per-asset EV flex headroom, gated by bidirectional charger allocation.

        Allocation rule: the household has ``_bidirectional_chargers`` DCEV
        bidirectional chargers shared by the EVs marked bidirectional_capable
        (the reference install: one charger, two EVs). Only an EV holding a
        charger contributes flex; a capable EV without one gets (0, 0).
        Allocation is sticky:
          - An EV in self._bidirectional_holders keeps its charger.
          - Else a plugged EV claims a free charger, in configured asset order
            so this is stable.
          - An unplugged or driving EV releases its charger.

        An EV that cannot use a bidirectional charger (or a household with
        none) is on its own charger: charge headroom up to
        DEFAULT_EV_MAX_CHARGE_KW, no V2G.

        Connection state gating:
          - 'unplugged' / 'driving' -> (0, 0): EV not present.
          - 'charge_only' -> available_down = 0 (no V2G).
          - 'bidirectional' -> both directions available.
          - 'plugged_idle' / 'charging' / 'discharging' -> use
            power_flow_capability to decide if down is available.

        Returns (available_up_kw, available_down_kw), both >= 0.
        """
        if connection_state in ("unplugged", "driving"):
            # Not at home: free its bidirectional charger for another EV.
            if asset_id in self._bidirectional_holders:
                self._bidirectional_holders.remove(asset_id)
            return 0.0, 0.0

        if not bidirectional_capable or self._bidirectional_chargers <= 0:
            current_charge_rate = max(0.0, ev_setpoint_kw)
            return max(0.0, DEFAULT_EV_MAX_CHARGE_KW - current_charge_rate), 0.0

        # DCEV allocation: only a sticky holder gets a charger.
        if asset_id not in self._bidirectional_holders:
            if len(self._bidirectional_holders) >= self._bidirectional_chargers:
                return 0.0, 0.0
            # A charger is free. Claim it for this plugged EV.
            self._bidirectional_holders.append(asset_id)

        max_charge = self._read_dcev_ac_to_dc()
        max_discharge = self._read_dcev_dc_to_ac()

        current_charge_rate = max(0.0, ev_setpoint_kw)
        current_discharge_rate = max(0.0, -ev_setpoint_kw)

        available_up = max(0.0, max_charge - current_charge_rate)
        if power_flow_capability == "bidirectional":
            available_down = max(0.0, max_discharge - current_discharge_rate)
        else:
            # charge_only or none: V2G not available, but charge headroom is.
            available_down = 0.0

        return available_up, available_down

    def _derive_flex_headroom(self, battery_setpoint_kw: float) -> tuple[float, float]:
        """Battery-only flex headroom (legacy fallback path).

        Used only when ``_build_record`` cannot aggregate per-asset flex (e.g.
        if asset records were not built this interval). Cohort flex is normally
        computed from the per-asset sum in ``_build_record`` itself.
        """
        if not self._flex_derived_logged:
            _LOGGER.info(
                "flex_available_up/down derived per-asset (battery + DCEV-allocated EVs) "
                "clipped to grid envelope. See _build_record for the sum-and-clip path."
            )
            self._flex_derived_logged = True

        return self._battery_asset_flex(battery_setpoint_kw)

    def _infer_ev_connection_state(
        self,
        asset_id: str,
        shadow: float | None,
        setpoint_kw: float | None,
        current_soc: float,
        now: datetime,
    ) -> tuple[str, str]:
        """Infer EV connection_state and power_flow_capability for one interval.

        Connection state inference rules (see spec section 6):
        - shadow is None/unavailable -> 'unplugged'
        - shadow present AND setpoint is None or ~0 AND SOC delta < 0.5% -> 'plugged_idle'
        - setpoint > 0 OR SOC rising > 1%/5min -> 'charging'
        - setpoint < 0 OR SOC falling > 1%/5min (while not driving) -> 'discharging'
        - shadow unavailable AND SOC dropping > 1.5%/5min -> 'driving'

        Power flow capability:
        - 'unplugged' -> 'none'
        - 'discharging' -> 'bidirectional' (marks this EV as last bidirectional user)
        - plugged AND no recent discharge -> 'charge_only'
        - plugged AND recent discharge (within sticky window) from THIS ev -> 'bidirectional'
        - plugged AND recent discharge from ANOTHER ev -> 'charge_only' (other EV has the bidirectional charger)

        Returns (connection_state, power_flow_capability).
        """
        now_utc = now

        # Retrieve previous SOC for delta calculation
        soc_delta_pct: float | None = None
        if asset_id in self._ev_prev_soc:
            prev_soc, prev_ts = self._ev_prev_soc[asset_id]
            elapsed_minutes = (now_utc - prev_ts).total_seconds() / 60.0
            if elapsed_minutes > 0:
                soc_delta_pct = current_soc - prev_soc  # positive = rising

        # Rule 1: shadow absent -> check for driving vs unplugged
        if shadow is None:
            if soc_delta_pct is not None and soc_delta_pct < -_SOC_DELTA_DRIVING_MIN:
                # SOC dropping rapidly without a shadow price: likely driving
                return "driving", "none"
            return "unplugged", "none"

        # Shadow is present: EV is plugged
        # Rule 2: charging (setpoint > 0 or SOC rising fast)
        if setpoint_kw is not None and setpoint_kw > 0.1:
            return "charging", self._get_power_flow_capability(asset_id, "charging")

        if soc_delta_pct is not None and soc_delta_pct > _SOC_DELTA_CHARGE_MIN:
            return "charging", self._get_power_flow_capability(asset_id, "charging")

        # Rule 3: discharging (setpoint < 0 or SOC falling fast)
        if setpoint_kw is not None and setpoint_kw < -0.1:
            self._record_discharge(asset_id, now_utc)
            return "discharging", "bidirectional"

        if soc_delta_pct is not None and soc_delta_pct < -_SOC_DELTA_DISCHARGE_MIN:
            self._record_discharge(asset_id, now_utc)
            return "discharging", "bidirectional"

        # Rule 4: plugged idle
        return "plugged_idle", self._get_power_flow_capability(asset_id, "plugged_idle")

    def _record_discharge(self, asset_id: str, now: datetime) -> None:
        """Record that this EV was observed discharging (bidirectional charger).

        A discharging EV is on a bidirectional charger, so it takes one; when
        all are held, the longest-held one is handed over.
        """
        self._last_discharge_time[asset_id] = now
        if self._bidirectional_chargers <= 0:
            return
        if asset_id in self._bidirectional_holders:
            self._bidirectional_holders.remove(asset_id)
        self._bidirectional_holders.append(asset_id)
        del self._bidirectional_holders[: -self._bidirectional_chargers]

    def _get_power_flow_capability(self, asset_id: str, connection_state: str) -> str:
        """Determine power_flow_capability based on sticky bidirectional tracking."""
        if connection_state == "charging":
            # If this EV was recently discharging, it has the bidirectional charger
            last_discharge = self._last_discharge_time.get(asset_id)
            if last_discharge is not None:
                hours_since = (datetime.now(UTC) - last_discharge).total_seconds() / 3600
                if hours_since <= _BIDIRECTIONAL_STICKY_HOURS:
                    return "bidirectional"
            # Conservative default until first discharge observed
            return "charge_only"

        if connection_state == "plugged_idle":
            # Same logic as charging
            last_discharge = self._last_discharge_time.get(asset_id)
            if last_discharge is not None:
                hours_since = (datetime.now(UTC) - last_discharge).total_seconds() / 3600
                if hours_since <= _BIDIRECTIONAL_STICKY_HOURS:
                    return "bidirectional"
            return "charge_only"

        return "charge_only"

    def _build_asset_record(
        self,
        asset_id: str,
        asset_spec: dict,
        now: datetime,
    ) -> dict[str, Any]:
        """Build a single asset record for one interval.

        Reads entity states, infers EV connection state, and updates SOC history.
        ``asset_spec`` is one entry of the configured asset list (#15).
        """
        kind: str = asset_spec["kind"]
        bidirectional_capable: bool = bool(asset_spec.get("bidirectional_capable", True))
        soc_entity: str | None = asset_spec.get("soc_entity")
        setpoint_entity: str | None = asset_spec.get("setpoint_entity")
        shadow_entity: str | None = asset_spec.get("shadow_entity")
        capacity_kwh: float = float(asset_spec.get("capacity_kwh") or 0.0)

        # Read entities
        soc_pct = _read_state_float(self.hass, soc_entity, fallback=0.0) or 0.0
        setpoint_kw = _read_state_float_or_none(self.hass, setpoint_entity)
        shadow = _read_state_float_or_none(self.hass, shadow_entity)
        sp = setpoint_kw if setpoint_kw is not None else 0.0

        # Derive per-asset flex headroom.
        # - Battery: clipped to hybrid inverter rating (PV + battery share AC side).
        # - EV: gated by bidirectional charger allocation and connection_state.
        #   Must compute connection_state first.
        connection_state: str | None = None
        power_flow_capability: str | None = None

        if kind == ASSET_KIND_BATTERY:
            available_up, available_down = self._battery_asset_flex(sp)
        elif kind == ASSET_KIND_EV:
            connection_state, power_flow_capability = self._infer_ev_connection_state(
                asset_id, shadow, setpoint_kw, soc_pct, now
            )
            available_up, available_down = self._ev_asset_flex(
                asset_id, sp, connection_state, power_flow_capability,
                bidirectional_capable=bidirectional_capable,
            )
        else:
            # Unknown asset kind: fall back to spec-declared limits, no clip.
            max_charge = asset_spec.get("max_charge_kw", DEFAULT_EV_MAX_CHARGE_KW)
            max_discharge = asset_spec.get("max_discharge_kw", DEFAULT_EV_MAX_DISCHARGE_KW)
            available_up = max(0.0, max_charge - max(0.0, sp))
            available_down = max(0.0, max_discharge - max(0.0, -sp))

        record: dict[str, Any] = {
            "asset_id": asset_id,
            "kind": kind,
            "bidirectional_capable": bidirectional_capable,
            "capacity_kwh": capacity_kwh,
            "soc_pct": soc_pct,
            "setpoint_kw": setpoint_kw,
            "available_up_kw": round(available_up, 3),
            "available_down_kw": round(available_down, 3),
            "shadow_power_balance_price": shadow,
        }

        # EV-specific fields
        if kind == ASSET_KIND_EV:
            record["connection_state"] = connection_state
            record["power_flow_capability"] = power_flow_capability
            record["departure_target_pct"] = None
            record["departure_time_utc"] = None

        # Update SOC history for next interval's delta calculation
        if kind == ASSET_KIND_EV:
            self._ev_prev_soc[asset_id] = (soc_pct, now)

        return record

    def _build_record(self) -> dict[str, Any]:
        """Read all HAEO entity states and build a schema v2.0 telemetry record.

        Prices are stored in $/kWh (no /1000 conversion).
        Runs in the main HA event loop (state reads are non-blocking).
        """
        now_utc = datetime.now(tz=UTC)
        minutes = (now_utc.minute // 5) * 5
        interval_start = now_utc.replace(minute=minutes, second=0, microsecond=0)

        # Core measurements
        net_import_kw: float = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_NET_IMPORT), fallback=0.0
        ) or 0.0

        solar_kw: float = max(
            0.0,
            _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_SOLAR), fallback=0.0
            ) or 0.0,
        )

        total_load_kw: float = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_TOTAL_LOAD), fallback=0.0
        ) or 0.0

        # house_load_kw = total_load - sum(deferrable current_kw), clamped to 0
        # deferrable_loads is empty in v0.3, so house_load_kw = total_load (clamped)
        deferrable_load_kw: float = 0.0
        house_load_kw: float = max(0.0, total_load_kw - deferrable_load_kw)

        # Prices in $/kWh (no conversion)
        price_signal_seen: float = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_PRICE_SIGNAL), fallback=0.0
        ) or 0.0
        price_export_seen: float = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_PRICE_EXPORT), fallback=0.0
        ) or 0.0

        # Envelope limits
        envelope_import_limit_kw: float = _read_state_float(
            self.hass, self._config.get(CONF_ENTITY_ENVELOPE_IMPORT), fallback=5.0
        ) or 5.0
        envelope_export_limit_kw: float = abs(
            _read_state_float(
                self.hass, self._config.get(CONF_ENTITY_ENVELOPE_EXPORT), fallback=5.0
            ) or 5.0
        )

        # Shadow prices (all nullable, all in $/kWh).
        #
        # shadow_energy_price is the headline switchboard power-balance dual,
        # i.e. the marginal cost of one extra kWh of net energy at the meter.
        # The four constraint-specific shadows are non-zero only when that
        # particular constraint is binding for the current interval.
        shadow_energy_price = _read_state_float_or_none(
            self.hass, self._config.get(CONF_ENTITY_SHADOW_ENERGY)
        )
        if shadow_energy_price is not None:
            shadow_energy_price = round(shadow_energy_price, 6)

        shadow_load_forecast = _read_state_float_or_none(
            self.hass, self._config.get(CONF_ENTITY_SHADOW_LOAD_FORECAST)
        )
        shadow_solar_forecast = _read_state_float_or_none(
            self.hass, self._config.get(CONF_ENTITY_SHADOW_SOLAR_FORECAST)
        )
        shadow_envelope_import = _read_state_float_or_none(
            self.hass, self._config.get(CONF_ENTITY_SHADOW_ENVELOPE_IMPORT)
        )
        shadow_envelope_export = _read_state_float_or_none(
            self.hass, self._config.get(CONF_ENTITY_SHADOW_ENVELOPE_EXPORT)
        )

        # Naive baseline: use total_load_kw (subtraction method)
        # If HAEO exposes a counterfactual sensor, it would go here in a future version
        naive_baseline_kw = total_load_kw
        naive_baseline_method = "subtraction"

        # Build asset records first: per-asset flex computation depends on
        # connection_state inference and DCEV sticky allocation. Only the
        # configured assets are published, and only while they exist (#15).
        assets: list[dict[str, Any]] = []
        for asset_spec in self._assets:
            asset_id = asset_spec["asset_id"]
            if not self._asset_present(asset_spec):
                continue
            try:
                asset_record = self._build_asset_record(asset_id, asset_spec, now_utc)
                assets.append(asset_record)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Failed to build asset record for %s: %s", asset_id, exc
                )

        # Household flex aggregation (schema v2.0).
        #
        # The household has three sequential bottlenecks on flex:
        #   1. asset_sum         - what the assets can physically deliver/absorb
        #   2. hybrid inverter   - AC-side ceiling (battery, PV, DCEV all share it)
        #   3. grid envelope     - DNSP-imposed import/export cap (CSIP-AUS or static)
        # Cohort flex is the minimum of the three.
        #
        # If HAEO exposes a household flex sensor directly, that takes precedence
        # (it presumably already accounts for these constraints inside HAEO's LP).
        flex_up_entity = self._config.get(CONF_ENTITY_FLEX_UP)
        flex_down_entity = self._config.get(CONF_ENTITY_FLEX_DOWN)

        inverter_ac_to_dc = self._read_inverter_ac_to_dc()
        inverter_dc_to_ac = self._read_inverter_dc_to_ac()

        if flex_up_entity and self.hass.states.get(flex_up_entity) is not None:
            flex_up = max(
                0.0,
                _read_state_float(self.hass, flex_up_entity, fallback=0.0) or 0.0,
            )
        else:
            asset_flex_up_sum = sum(
                a.get("available_up_kw", 0.0) or 0.0 for a in assets
            )
            flex_up = min(
                asset_flex_up_sum,
                inverter_ac_to_dc,
                envelope_import_limit_kw,
            )

        if flex_down_entity and self.hass.states.get(flex_down_entity) is not None:
            flex_down = max(
                0.0,
                _read_state_float(self.hass, flex_down_entity, fallback=0.0) or 0.0,
            )
        else:
            asset_flex_down_sum = sum(
                a.get("available_down_kw", 0.0) or 0.0 for a in assets
            )
            flex_down = min(
                asset_flex_down_sum,
                inverter_dc_to_ac,
                envelope_export_limit_kw,
            )

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "interval_start_utc": interval_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": self.region,
            "postcode_prefix": self._postcode_prefix,
            "net_import_kw": round(net_import_kw, 3),
            "solar_kw": round(solar_kw, 3),
            "house_load_kw": round(house_load_kw, 3),
            "deferrable_load_kw": round(deferrable_load_kw, 3),
            "naive_baseline_kw": round(naive_baseline_kw, 3),
            "naive_baseline_method": naive_baseline_method,
            "price_signal_seen": round(price_signal_seen, 6),
            "price_export_seen": round(price_export_seen, 6),
            "envelope_import_limit_kw": round(envelope_import_limit_kw, 3),
            "envelope_export_limit_kw": round(envelope_export_limit_kw, 3),
            "flex_available_up_kw": round(flex_up, 3),
            "flex_available_down_kw": round(flex_down, 3),
            "shadow_energy_price": shadow_energy_price,
            "shadow_load_forecast_price": shadow_load_forecast,
            "shadow_solar_forecast_price": shadow_solar_forecast,
            "shadow_envelope_import_price": shadow_envelope_import,
            "shadow_envelope_export_price": shadow_envelope_export,
            "assets": assets,
            "deferrable_loads": [],
        }
        return record

    def _asset_present(self, asset_spec: dict[str, Any]) -> bool:
        """Return whether an asset's SOC entity exists (#15).

        A missing asset is left out of the record rather than published with
        a zero SOC. It is logged once (at DEBUG during the startup grace
        period, when the entity may simply not be registered yet), and its
        return is logged at INFO.
        """
        asset_id = asset_spec["asset_id"]
        soc_entity = asset_spec.get("soc_entity")
        if soc_entity and self.hass.states.get(soc_entity) is not None:
            if asset_id in self._missing_assets:
                self._missing_assets.discard(asset_id)
                _LOGGER.info("Asset %s is available again (%s)", asset_id, soc_entity)
            return True
        if self._intervals_seen <= STARTUP_GRACE_INTERVALS:
            _LOGGER.debug(
                "Asset %s not ready yet (%s); leaving it out of this interval",
                asset_id, soc_entity or "no SOC entity mapped",
            )
        elif asset_id not in self._missing_assets:
            self._missing_assets.add(asset_id)
            _LOGGER.warning(
                "Asset %s left out of telemetry: SOC entity %s does not exist. "
                "Check the batteries and EVs in the integration options.",
                asset_id, soc_entity or "(not mapped)",
            )
        return False

    async def _async_run_global_sweep(self) -> None:
        """Run the global entity sweep and log unmapped entities."""
        asset_entities = {
            a[key]
            for a in self._assets
            for key in ("soc_entity", "setpoint_entity", "shadow_entity")
            if a.get(key)
        }
        unmapped = run_global_sweep(self.hass, extra_mapped=asset_entities)
        self._last_sweep_unmapped = unmapped
        self._data.unmapped_entities = unmapped
        if unmapped:
            _LOGGER.info(
                "Global sweep found %d unmapped entit%s on startup: %s",
                len(unmapped),
                "y" if len(unmapped) == 1 else "ies",
                ", ".join(unmapped),
            )

    async def _async_discover_context(self) -> None:
        """Discover context entities at first update and log them."""
        self._context_entities = await discover_context_entities(
            self.hass, region=self.region
        )
        self._context_discovered = True
        _LOGGER.info(
            "Context entities: %s",
            {k: v for k, v in self._context_entities.items() if v is not None},
        )

    async def _async_update_data(self) -> CoordinatorData:
        """Poll HAEO entities, buffer the record, and push to GitHub when due.

        Called automatically every UPDATE_INTERVAL_SECONDS by the base class.
        Triggers re-authentication if the stored token is rejected by GitHub.
        """
        if self.source == SOURCE_NIMBUS:
            return await self._async_update_from_nimbus()

        # One-time context entity discovery
        if not self._context_discovered:
            await self._async_discover_context()
            await self._async_run_global_sweep()

        self._intervals_seen += 1

        # Power-rating health-check: logs live vs fallback status changes.
        self._log_power_rating_health_check()

        # Do not publish an interval built from unavailable inputs: the
        # readers fall back to 0.0, which looks like real data (#21).
        missing = self._missing_required_inputs()
        if missing:
            self._skipped_intervals += 1
            if self._intervals_seen <= STARTUP_GRACE_INTERVALS:
                _LOGGER.debug("Skipping interval; inputs not ready: %s", ", ".join(missing))
            elif frozenset(missing) != self._missing_warned:
                _LOGGER.warning(
                    "Skipping telemetry interval: required input(s) unavailable: %s. "
                    "Check the entity mapping in the integration options.",
                    ", ".join(missing),
                )
                # Only remember what was actually reported, so inputs missing
                # during the grace period still warn once it ends.
                self._missing_warned = frozenset(missing)
            self._data.skipped_intervals = self._skipped_intervals
            return self._data
        if self._missing_warned:
            _LOGGER.info("Required inputs available again; resuming telemetry")
            self._missing_warned = frozenset()

        record = self._build_record()

        # Validate using voluptuous (synchronous; run in executor)
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, record
            )
        except vol.Invalid as exc:
            # Skip this interval and count it; do not fail the update, which
            # would mark every diagnostic sensor unavailable (#30).
            self._note_validation_error(exc, record)
            return await self._async_after_buffer()

        # A quick restart can rebuild an interval already restored from
        # storage; keep the first copy (#18).
        if any(
            r.get("interval_start_utc") == validated["interval_start_utc"]
            for r in self._buffer
        ):
            _LOGGER.debug("Interval %s already buffered", validated["interval_start_utc"])
        else:
            self._buffer.append(validated)
        _LOGGER.debug(
            "Buffered record %s (buffer size: %d)",
            validated["interval_start_utc"],
            len(self._buffer),
        )
        return await self._async_after_buffer()

    async def _async_after_buffer(self) -> CoordinatorData:
        """Push when due and refresh the sensor-facing counters."""
        self._data.source = self.source
        self._roll_push_day()

        # Push when buffer reaches RECORDS_PER_PUSH
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()

        await self._async_refresh_cohort_size()
        self._sync_data()
        await self._async_save_state()
        return self._data

    # ------------------------------------------------------------------
    # Persistent state (#18, #19)
    # ------------------------------------------------------------------

    def _roll_push_day(self) -> None:
        """Reset the daily counter at local midnight (HA time zone)."""
        today = dt_util.now().date().isoformat()
        if self._push_day != today:
            if self._push_day is not None:
                self._records_pushed_today = 0
            self._push_day = today

    def _note_validation_error(self, exc: vol.Invalid, record: dict[str, Any]) -> None:
        """Count a rejected record; warn once per failing field (#30)."""
        self._validation_error_count += 1
        field = ".".join(str(p) for p in exc.path) or "record"
        value = record
        for part in exc.path:
            try:
                value = value[part]
            except (KeyError, IndexError, TypeError):
                value = None
                break
        if field not in self._invalid_warned:
            self._invalid_warned.add(field)
            _LOGGER.warning(
                "Skipping telemetry interval %s: %s (value %r). Further failures "
                "on this field are counted in the validation_errors attribute.",
                record.get("interval_start_utc"),
                exc,
                value,
            )
        _LOGGER.debug("Rejected record: %s", record)

    def _sync_data(self) -> None:
        """Copy internal counters into the sensor-facing data object."""
        self._data.buffer_size = len(self._buffer)
        self._data.records_pushed_today = self._records_pushed_today
        self._data.push_errors = self._push_error_count
        self._data.validation_errors = self._validation_error_count

    async def async_load_state(self) -> None:
        """Restore the buffer and push stats saved before the last restart."""
        try:
            stored = await self._store.async_load()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Could not load stored telemetry state: %s", exc)
            return
        if not stored:
            return

        records = stored.get("buffer") or []
        for record in records[-BUFFER_MAX_RECORDS:]:
            self._buffer.append(record)

        stats = stored.get("stats") or {}
        self._records_pushed_today = int(stats.get("records_pushed_today", 0))
        self._push_day = stats.get("push_day")
        self._push_error_count = int(stats.get("push_errors", 0))
        self._validation_error_count = int(stats.get("validation_errors", 0))
        self._data.cohort_size = int(stats.get("cohort_size", 0))
        if stats.get("last_push_time"):
            self._data.last_push_time = dt_util.parse_datetime(stats["last_push_time"])
        if stats.get("cohort_checked"):
            self._cohort_checked = dt_util.parse_datetime(stats["cohort_checked"])
        self._roll_push_day()
        self._sync_data()

        if records:
            _LOGGER.info(
                "Restored %d buffered record(s) for household %s from storage",
                len(self._buffer),
                self.household_id,
            )

    async def _async_save_state(self) -> None:
        """Persist the buffer and push stats."""
        last_push = self._data.last_push_time
        payload = {
            "buffer": list(self._buffer),
            "stats": {
                "records_pushed_today": self._records_pushed_today,
                "push_day": self._push_day,
                "push_errors": self._push_error_count,
                "validation_errors": self._validation_error_count,
                "cohort_size": self._data.cohort_size,
                "last_push_time": last_push.isoformat() if last_push else None,
                "cohort_checked": (
                    self._cohort_checked.isoformat() if self._cohort_checked else None
                ),
            },
        }
        try:
            await self._store.async_save(payload)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Could not save telemetry state: %s", exc)

    async def _async_refresh_cohort_size(self) -> None:
        """Refresh cohort size from the published status.json every 6 hours."""
        now = datetime.now(tz=UTC)
        if self._cohort_checked and now - self._cohort_checked < COHORT_REFRESH:
            return
        self._cohort_checked = now
        try:
            size = await self._get_or_create_github_client().get_cohort_size()
        except TokenInvalidError as exc:
            _LOGGER.warning(
                "Token invalid while fetching cohort size: %s. Triggering reauth.", exc
            )
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not update cohort size: %s", exc)
            return
        if size is not None:
            self._data.cohort_size = size

    async def async_handle_stop(self, _event: Event | None = None) -> None:
        """Save state on Home Assistant stop, then try one final push.

        Home Assistant does not unload config entries on stop, so
        ``async_shutdown`` alone never ran on a restart (#18). The save comes
        first so nothing is lost if the push times out.
        """
        await self._async_save_state()
        try:
            async with asyncio.timeout(STOP_PUSH_TIMEOUT_S):
                await self._async_push_buffer()
        except TimeoutError:
            _LOGGER.info("Final push timed out; %d record(s) kept for next start",
                         len(self._buffer))
        await self._async_save_state()

    # ------------------------------------------------------------------
    # Nimbus source (#27, nimbus#1634)
    # ------------------------------------------------------------------

    @callback
    def async_start_nimbus_listener(self) -> bool:
        """Relay each new Nimbus record as soon as Nimbus publishes it.

        Nimbus changes the sensor state once per completed interval. The
        5-minute poll in ``_async_update_from_nimbus`` is a backstop for a
        missed event; duplicates are dropped on ``interval_start_utc``.
        """
        if self.source != SOURCE_NIMBUS:
            return False

        async def _ingest_and_notify() -> None:
            if await self._async_ingest_nimbus():
                self._data.records_pushed_today = self._records_pushed_today
                self._data.push_errors = self._push_error_count
            self.async_update_listeners()

        @callback
        def _on_change(event: Event) -> None:
            self.hass.async_create_task(_ingest_and_notify())

        self._nimbus_unsub = async_track_state_change_event(
            self.hass, [NIMBUS_TELEMETRY_ENTITY], _on_change
        )
        return True

    @callback
    def async_stop_nimbus_listener(self) -> None:
        """Stop relaying Nimbus records (idempotent)."""
        if self._nimbus_unsub is not None:
            self._nimbus_unsub()
            self._nimbus_unsub = None

    async def _async_ingest_nimbus(self) -> bool:
        """Buffer the current Nimbus record if it is new and valid."""
        result = read_nimbus_record(
            self.hass.states.get(NIMBUS_TELEMETRY_ENTITY),
            region=self.region,
            postcode_prefix=self._postcode_prefix,
        )
        if result.record is None:
            if result.reason != self._data.source_status:
                _LOGGER.warning("Nimbus source: %s", result.reason)
            self._data.source_status = result.reason
            return False

        interval = result.record["interval_start_utc"]
        if any(r.get("interval_start_utc") == interval for r in self._buffer):
            return False
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, result.record
            )
        except vol.Invalid as exc:
            self._note_validation_error(exc, result.record)
            self._data.source_status = f"Nimbus record for {interval} failed validation: {exc}"
            self._data.validation_errors = self._validation_error_count
            return False

        if self._data.source_status:
            _LOGGER.info("Nimbus source: records available again")
        self._data.source_status = None
        self._buffer.append(validated)
        _LOGGER.debug("Buffered Nimbus record %s (buffer size: %d)", interval, len(self._buffer))
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()
        self._data.buffer_size = len(self._buffer)
        return True

    async def _async_update_from_nimbus(self) -> CoordinatorData:
        """5-minute backstop for the Nimbus source, then the usual push."""
        await self._async_ingest_nimbus()
        return await self._async_after_buffer()

    def _validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run lightweight voluptuous validation on the top-level record.

        Full JSON Schema validation (additionalProperties etc.) is run by the
        CI workflow via jsonschema. Here we just confirm critical fields are
        present and prices are in plausible $/kWh range.

        Market prices use the -2.0 to 20.0 $/kWh window. LP duals (shadow_*)
        are not market prices: they grow without a price-cap bound when a hard
        constraint binds, so they only get a wide sanity guard (#30).
        """
        _NEM_REGIONS = vol.In(["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
        _PRICE_RANGE = vol.All(vol.Coerce(float), vol.Range(min=-2.0, max=20.0))
        _SHADOW_RANGE = vol.All(
            vol.Coerce(float), vol.Range(min=SHADOW_PRICE_MIN, max=SHADOW_PRICE_MAX)
        )
        _KW_NON_NEG = vol.All(vol.Coerce(float), vol.Range(min=0))
        _ASSET = vol.Schema(
            {vol.Optional("shadow_power_balance_price"): vol.Any(None, _SHADOW_RANGE)},
            extra=vol.ALLOW_EXTRA,
        )

        schema = vol.Schema(
            {
                vol.Required("schema_version"): "2.0",
                vol.Required("interval_start_utc"): str,
                vol.Required("region"): _NEM_REGIONS,
                vol.Required("postcode_prefix"): vol.Match(r"^[0-9]{3}$"),
                vol.Required("net_import_kw"): vol.Coerce(float),
                vol.Required("solar_kw"): _KW_NON_NEG,
                vol.Required("house_load_kw"): _KW_NON_NEG,
                vol.Required("deferrable_load_kw"): _KW_NON_NEG,
                vol.Required("naive_baseline_kw"): vol.Coerce(float),
                vol.Required("naive_baseline_method"): vol.In(
                    ["subtraction", "haeo_counterfactual"]
                ),
                vol.Required("price_signal_seen"): _PRICE_RANGE,
                vol.Required("price_export_seen"): _PRICE_RANGE,
                vol.Required("envelope_import_limit_kw"): _KW_NON_NEG,
                vol.Required("envelope_export_limit_kw"): _KW_NON_NEG,
                vol.Required("flex_available_up_kw"): _KW_NON_NEG,
                vol.Required("flex_available_down_kw"): _KW_NON_NEG,
                vol.Optional("shadow_energy_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_load_forecast_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_solar_forecast_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_envelope_import_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_envelope_export_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Required("assets"): [_ASSET],
                vol.Required("deferrable_loads"): list,
            },
            extra=vol.ALLOW_EXTRA,
        )
        return schema(record)

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
            if self._no_push_access:
                self._no_push_access = False
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_PUSH_ACCESS)
                _LOGGER.info("Write access to %s confirmed; pushes resumed", GITHUB_REPO)
            _LOGGER.info(
                "Pushed %d records for household %s (schema v%s, v%s)",
                count,
                self.household_id,
                SCHEMA_VERSION,
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

        except PushPermissionError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            if not self._no_push_access:
                # Log and raise a repair once; keep buffering until access is
                # granted (#14). Up to 24 hours of records are kept.
                self._no_push_access = True
                _LOGGER.error("%s Records are kept and retried each hour.", exc)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_NO_PUSH_ACCESS,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_NO_PUSH_ACCESS,
                    translation_placeholders={"repo": GITHUB_REPO},
                    learn_more_url=(
                        "https://github.com/purcell-lab/nem-flex-telemetry/blob/main/"
                        "docs/INSTALL.md#push-fails-with-http-404"
                    ),
                )

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
        self._sync_data()
        await self._async_save_state()
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Attempt a final flush before unloading."""
        self.async_stop_nimbus_listener()
        _LOGGER.info(
            "Coordinator shutting down, attempting final push for %s",
            self.household_id,
        )
        await self._async_push_buffer()
        await self._async_save_state()
        await super().async_shutdown()
