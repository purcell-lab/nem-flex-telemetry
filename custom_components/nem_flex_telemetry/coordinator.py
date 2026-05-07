"""DataUpdateCoordinator for NEM Flex Telemetry.

Responsibilities:
- Read HAEO entity states every 5 minutes
- Build and validate the schema v2.0 telemetry record (18 flat fields + assets[] + deferrable_loads[])
- Derive flex headroom from battery limits when HAEO does not expose them directly
- Infer per-EV connection state and power_flow_capability (not from any entity)
- Track the last_bidirectional_ev_id across intervals (sticky)
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

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ASSET_DEFAULTS,
    CONF_EV1_CAPACITY_KWH,
    CONF_EV2_CAPACITY_KWH,
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
    CONF_HOME_BATTERY_CAPACITY_KWH,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_BATTERY_MAX_CHARGE_KW,
    DEFAULT_BATTERY_MAX_DISCHARGE_KW,
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
    RECORDS_PER_PUSH,
    SCHEMA_VERSION,
    UPDATE_INTERVAL_SECONDS,
    VERSION,
)
from .discovery import discover_context_entities, run_global_sweep
from .github_client import GitHubPushError, NemFlexGitHubClient, TokenInvalidError

_LOGGER = logging.getLogger(__name__)

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
        self.unmapped_entities: list[str] = []


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

        # Flex derivation logging gate
        self._flex_derived_logged: bool = False

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

        # Which EV was last confirmed on the bidirectional charger
        self._last_bidirectional_ev_id: str | None = None

        # Asset capacity overrides from config (set in async_step_assets)
        self._asset_capacity: dict[str, float] = {}
        if CONF_HOME_BATTERY_CAPACITY_KWH in self._config:
            self._asset_capacity["home_battery"] = float(
                self._config[CONF_HOME_BATTERY_CAPACITY_KWH]
            )
        if CONF_EV1_CAPACITY_KWH in self._config:
            self._asset_capacity["ev1"] = float(self._config[CONF_EV1_CAPACITY_KWH])
        if CONF_EV2_CAPACITY_KWH in self._config:
            self._asset_capacity["ev2"] = float(self._config[CONF_EV2_CAPACITY_KWH])

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

    def _battery_asset_flex(
        self, battery_setpoint_kw: float
    ) -> tuple[float, float]:
        """Per-asset battery flex headroom, clipped to the hybrid inverter rating.

        The battery shares a hybrid inverter with the PV array, so its real
        AC-side limit is min(battery_max_*, inverter_*).

        Returns (available_up_kw, available_down_kw), both >= 0.
        """
        max_charge = min(
            self._read_battery_max_charge(), self._read_inverter_ac_to_dc()
        )
        max_discharge = min(
            self._read_battery_max_discharge(), self._read_inverter_dc_to_ac()
        )
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
    ) -> tuple[float, float]:
        """Per-asset EV flex headroom, gated by DCEV charger allocation.

        Allocation rule: the household has ONE DCEV bidirectional charger shared
        across both EVs. Only the EV currently allocated the charger contributes
        flex; the other gets (0, 0). Allocation rule (sticky):
          - If self._last_bidirectional_ev_id is set and matches asset_id, this
            EV holds the charger.
          - Else the first plugged EV in iteration order wins. _build_record
            iterates ASSET_DEFAULTS deterministically so this is stable.

        Connection state gating:
          - 'unplugged' / 'driving' -> (0, 0): EV not present.
          - 'charge_only' -> available_down = 0 (no V2G).
          - 'bidirectional' -> both directions available.
          - 'plugged_idle' / 'charging' / 'discharging' -> use
            power_flow_capability to decide if down is available.

        Returns (available_up_kw, available_down_kw), both >= 0.
        """
        if connection_state in ("unplugged", "driving"):
            return 0.0, 0.0

        # DCEV allocation: only the sticky owner gets the charger.
        if (
            self._last_bidirectional_ev_id is not None
            and self._last_bidirectional_ev_id != asset_id
        ):
            return 0.0, 0.0
        if self._last_bidirectional_ev_id is None:
            # No sticky owner yet. Claim it for the first plugged EV.
            self._last_bidirectional_ev_id = asset_id

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
        """Record that this EV was observed discharging (bidirectional charger)."""
        self._last_discharge_time[asset_id] = now
        self._last_bidirectional_ev_id = asset_id

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
        """
        kind: str = asset_spec["kind"]
        bidirectional_capable: bool = asset_spec["bidirectional_capable"]
        soc_entity: str | None = asset_spec.get("soc_entity")
        setpoint_entity: str | None = asset_spec.get("setpoint_entity")
        shadow_entity: str | None = asset_spec.get("shadow_entity")

        # Capacity: config override takes precedence over spec default
        capacity_kwh: float = self._asset_capacity.get(
            asset_id, asset_spec.get("capacity_kwh", 0.0)
        )

        # Read entities
        soc_pct = _read_state_float(self.hass, soc_entity, fallback=0.0) or 0.0
        setpoint_kw = _read_state_float_or_none(self.hass, setpoint_entity)
        shadow = _read_state_float_or_none(self.hass, shadow_entity)
        sp = setpoint_kw if setpoint_kw is not None else 0.0

        # Derive per-asset flex headroom.
        # - Battery: clipped to hybrid inverter rating (PV + battery share AC side).
        # - EV: gated by DCEV charger allocation (one charger, two EVs) and
        #   connection_state. Must compute connection_state first.
        connection_state: str | None = None
        power_flow_capability: str | None = None

        if kind == "stationary_battery":
            available_up, available_down = self._battery_asset_flex(sp)
        elif kind == "ev":
            connection_state, power_flow_capability = self._infer_ev_connection_state(
                asset_id, shadow, setpoint_kw, soc_pct, now
            )
            available_up, available_down = self._ev_asset_flex(
                asset_id, sp, connection_state, power_flow_capability
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
        if kind == "ev":
            record["connection_state"] = connection_state
            record["power_flow_capability"] = power_flow_capability
            record["departure_target_pct"] = None
            record["departure_time_utc"] = None

        # Update SOC history for next interval's delta calculation
        if kind == "ev":
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
        # connection_state inference and DCEV sticky allocation.
        assets: list[dict[str, Any]] = []
        for asset_id, asset_spec in ASSET_DEFAULTS.items():
            try:
                asset_record = self._build_asset_record(asset_id, asset_spec, now_utc)
                assets.append(asset_record)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Failed to build asset record for %s: %s", asset_id, exc
                )

        # Household flex aggregation (schema v2.0):
        #   flex = sum(asset.available_*) clipped to grid envelope.
        # If HAEO exposes a flex sensor directly, that takes precedence.
        flex_up_entity = self._config.get(CONF_ENTITY_FLEX_UP)
        flex_down_entity = self._config.get(CONF_ENTITY_FLEX_DOWN)

        if flex_up_entity and self.hass.states.get(flex_up_entity) is not None:
            flex_up = max(
                0.0,
                _read_state_float(self.hass, flex_up_entity, fallback=0.0) or 0.0,
            )
        else:
            asset_flex_up_sum = sum(
                a.get("available_up_kw", 0.0) or 0.0 for a in assets
            )
            flex_up = min(asset_flex_up_sum, envelope_import_limit_kw)

        if flex_down_entity and self.hass.states.get(flex_down_entity) is not None:
            flex_down = max(
                0.0,
                _read_state_float(self.hass, flex_down_entity, fallback=0.0) or 0.0,
            )
        else:
            asset_flex_down_sum = sum(
                a.get("available_down_kw", 0.0) or 0.0 for a in assets
            )
            flex_down = min(asset_flex_down_sum, envelope_export_limit_kw)

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

    async def _async_run_global_sweep(self) -> None:
        """Run the global entity sweep and log unmapped entities."""
        unmapped = run_global_sweep(self.hass)
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
        # One-time context entity discovery
        if not self._context_discovered:
            await self._async_discover_context()
            await self._async_run_global_sweep()

        record = self._build_record()

        # Validate using voluptuous (synchronous; run in executor)
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

        # Push when buffer reaches RECORDS_PER_PUSH
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

    def _validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run lightweight voluptuous validation on the top-level record.

        Full JSON Schema validation (additionalProperties etc.) is run by the
        CI workflow via jsonschema. Here we just confirm critical fields are
        present and prices are in plausible $/kWh range.
        """
        _NEM_REGIONS = vol.In(["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
        _PRICE_RANGE = vol.All(vol.Coerce(float), vol.Range(min=-2.0, max=20.0))
        _KW_NON_NEG = vol.All(vol.Coerce(float), vol.Range(min=0))

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
                vol.Optional("shadow_energy_price"): vol.Any(None, _PRICE_RANGE),
                vol.Optional("shadow_load_forecast_price"): vol.Any(None, _PRICE_RANGE),
                vol.Optional("shadow_solar_forecast_price"): vol.Any(None, _PRICE_RANGE),
                vol.Optional("shadow_envelope_import_price"): vol.Any(None, _PRICE_RANGE),
                vol.Optional("shadow_envelope_export_price"): vol.Any(None, _PRICE_RANGE),
                vol.Required("assets"): list,
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
