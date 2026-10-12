"""Build one schema v2.0 telemetry record from entity states.

The same field logic serves two callers:

- the live coordinator, which reads ``hass.states`` once per interval, and
- the history rebuild (``rebuild.py``), which replays recorder history and
  reads the state each entity had at the moment the live builder would have
  read it.

The builder never touches Home Assistant directly. It reads states through
``_get_state(entity_id)``, which returns a State-like object (``.state``) or
None, so a subclass decides where states come from.

Stateful parts (EV SOC deltas, the sticky bidirectional charger owner, the
recent-discharge window) live on the builder instance. The live coordinator
carries them across intervals; a rebuild starts a fresh builder and replays
intervals in time order, so they are derived from the historical sequence.

Kept free of Home Assistant imports so it is testable directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .const import (
    ASSET_DEFAULTS,
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
    CONF_EV1_CAPACITY_KWH,
    CONF_EV2_CAPACITY_KWH,
    CONF_HOME_BATTERY_CAPACITY_KWH,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    DEFAULT_BATTERY_MAX_CHARGE_KW,
    DEFAULT_BATTERY_MAX_DISCHARGE_KW,
    DEFAULT_DCEV_AC_TO_DC_KW,
    DEFAULT_DCEV_DC_TO_AC_KW,
    DEFAULT_EV_MAX_CHARGE_KW,
    DEFAULT_EV_MAX_DISCHARGE_KW,
    DEFAULT_INVERTER_AC_TO_DC_KW,
    DEFAULT_INVERTER_DC_TO_AC_KW,
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_DCEV_AC_TO_DC,
    ENTITY_DCEV_DC_TO_AC,
    ENTITY_INVERTER_AC_TO_DC,
    ENTITY_INVERTER_DC_TO_AC,
    REQUIRED_ENTITY_FIELDS,
    SCHEMA_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# EV connection state inference constants
_SOC_DELTA_PLUGGED_IDLE_MAX = 0.5     # % per interval; below this = plugged_idle
_SOC_DELTA_CHARGE_MIN = 1.0           # % per 5min; rising at this rate = charging
_SOC_DELTA_DISCHARGE_MIN = 1.0        # % per 5min; falling at this rate = discharging
_SOC_DELTA_DRIVING_MIN = 1.5          # % per 5min; rapid drop with no shadow = driving
_BIDIRECTIONAL_STICKY_HOURS = 1       # hours; once seen discharging, stays bidirectional for this long

# Mapped entity fields the builder reads (besides ASSET_DEFAULTS and ratings).
MAPPED_ENTITY_FIELDS: tuple[str, ...] = (
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_SHADOW_ENERGY,
    CONF_ENTITY_SHADOW_LOAD_FORECAST,
    CONF_ENTITY_SHADOW_SOLAR_FORECAST,
    CONF_ENTITY_SHADOW_ENVELOPE_IMPORT,
    CONF_ENTITY_SHADOW_ENVELOPE_EXPORT,
)
RATING_ENTITIES: tuple[str, ...] = (
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_INVERTER_AC_TO_DC,
    ENTITY_INVERTER_DC_TO_AC,
    ENTITY_DCEV_AC_TO_DC,
    ENTITY_DCEV_DC_TO_AC,
)

_UNUSABLE = ("unavailable", "unknown", "")


def interval_start_for(now_utc: datetime) -> datetime:
    """Return the start of the 5-minute interval containing ``now_utc``."""
    minutes = (now_utc.minute // 5) * 5
    return now_utc.replace(minute=minutes, second=0, microsecond=0)


def format_interval(interval_start: datetime) -> str:
    """Format an interval start as it appears in ``interval_start_utc``."""
    return interval_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def asset_capacities(config: dict[str, Any]) -> dict[str, float]:
    """Asset capacity overrides from config (set in async_step_assets)."""
    capacity: dict[str, float] = {}
    if CONF_HOME_BATTERY_CAPACITY_KWH in config:
        capacity["home_battery"] = float(config[CONF_HOME_BATTERY_CAPACITY_KWH])
    if CONF_EV1_CAPACITY_KWH in config:
        capacity["ev1"] = float(config[CONF_EV1_CAPACITY_KWH])
    if CONF_EV2_CAPACITY_KWH in config:
        capacity["ev2"] = float(config[CONF_EV2_CAPACITY_KWH])
    return capacity


class RecordBuilder:
    """Field logic for one telemetry record. Subclasses supply ``_get_state``."""

    _config: dict[str, Any]
    region: str
    _postcode_prefix: str

    def _init_builder(self, config: dict[str, Any]) -> None:
        """Set the configuration and reset the cross-interval inference state."""
        self._config = config
        self.region = config[CONF_REGION]
        self._postcode_prefix = config[CONF_POSTCODE_PREFIX]
        self._asset_capacity: dict[str, float] = asset_capacities(config)

        # Flex derivation logging gate
        self._flex_derived_logged: bool = False

        # Per-EV SOC history for connection state inference
        # {asset_id: (prev_soc, prev_timestamp)}
        self._ev_prev_soc: dict[str, tuple[float, datetime]] = {}

        # Bidirectional charger sticky tracking
        # {asset_id: last_discharge_utc}
        self._last_discharge_time: dict[str, datetime] = {}

        # Which EV was last confirmed on the bidirectional charger
        self._last_bidirectional_ev_id: str | None = None

    def _get_state(self, entity_id: str) -> Any:
        """Return the State-like object for ``entity_id``, or None."""
        raise NotImplementedError

    def entities_read(self) -> list[str]:
        """Every entity ID ``_build_record`` can read, for history queries."""
        entity_ids: list[str] = []
        for field in MAPPED_ENTITY_FIELDS:
            entity_id = self._config.get(field)
            if entity_id:
                entity_ids.append(entity_id)
        for spec in ASSET_DEFAULTS.values():
            for key in ("soc_entity", "setpoint_entity", "shadow_entity"):
                if spec.get(key):
                    entity_ids.append(spec[key])
        entity_ids.extend(RATING_ENTITIES)
        return list(dict.fromkeys(entity_ids))

    def _state_float(
        self, entity_id: str | None, fallback: float | None = 0.0
    ) -> float | None:
        """Read an entity state as a float.

        Returns fallback if entity_id is None, entity is absent, or state
        is unavailable/unknown/unparseable.
        """
        if not entity_id:
            return fallback
        state = self._get_state(entity_id)
        if state is None or state.state in _UNUSABLE:
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

    def _state_float_or_none(self, entity_id: str | None) -> float | None:
        """Read an entity state as a float, returning None if unavailable."""
        if not entity_id:
            return None
        state = self._get_state(entity_id)
        if state is None or state.state in _UNUSABLE:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _read_number_kw(self, entity_id: str, default_kw: float) -> float:
        """Read a number.* power-rating entity in kW, with default fallback.

        Used for battery / inverter / DCEV charger power ratings.
        """
        state = self._get_state(entity_id)
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
        state = self._get_state(entity_id)
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                return abs(float(state.state)), True
            except ValueError:
                pass
        return default_kw, False

    def _missing_required_inputs(self) -> list[str]:
        """Return required entity fields whose entity has no usable state."""
        missing: list[str] = []
        for field in REQUIRED_ENTITY_FIELDS:
            entity_id = self._config.get(field)
            if self._state_float_or_none(entity_id) is None:
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
            return "charging", self._get_power_flow_capability(asset_id, "charging", now_utc)

        if soc_delta_pct is not None and soc_delta_pct > _SOC_DELTA_CHARGE_MIN:
            return "charging", self._get_power_flow_capability(asset_id, "charging", now_utc)

        # Rule 3: discharging (setpoint < 0 or SOC falling fast)
        if setpoint_kw is not None and setpoint_kw < -0.1:
            self._record_discharge(asset_id, now_utc)
            return "discharging", "bidirectional"

        if soc_delta_pct is not None and soc_delta_pct < -_SOC_DELTA_DISCHARGE_MIN:
            self._record_discharge(asset_id, now_utc)
            return "discharging", "bidirectional"

        # Rule 4: plugged idle
        return "plugged_idle", self._get_power_flow_capability(asset_id, "plugged_idle", now_utc)

    def _record_discharge(self, asset_id: str, now: datetime) -> None:
        """Record that this EV was observed discharging (bidirectional charger)."""
        self._last_discharge_time[asset_id] = now
        self._last_bidirectional_ev_id = asset_id

    def _get_power_flow_capability(
        self, asset_id: str, connection_state: str, now: datetime | None = None
    ) -> str:
        """Determine power_flow_capability based on sticky bidirectional tracking.

        ``now`` is the time the interval was read; a history rebuild passes the
        replayed time so the sticky window is measured in interval time.
        """
        now = now or datetime.now(UTC)
        if connection_state == "charging":
            # If this EV was recently discharging, it has the bidirectional charger
            last_discharge = self._last_discharge_time.get(asset_id)
            if last_discharge is not None:
                hours_since = (now - last_discharge).total_seconds() / 3600
                if hours_since <= _BIDIRECTIONAL_STICKY_HOURS:
                    return "bidirectional"
            # Conservative default until first discharge observed
            return "charge_only"

        if connection_state == "plugged_idle":
            # Same logic as charging
            last_discharge = self._last_discharge_time.get(asset_id)
            if last_discharge is not None:
                hours_since = (now - last_discharge).total_seconds() / 3600
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
        soc_pct = self._state_float(soc_entity, fallback=0.0) or 0.0
        setpoint_kw = self._state_float_or_none(setpoint_entity)
        shadow = self._state_float_or_none(shadow_entity)
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

    def _build_record(self, now_utc: datetime | None = None) -> dict[str, Any]:
        """Read all HAEO entity states and build a schema v2.0 telemetry record.

        ``now_utc`` is the moment the states are read: the current time for
        the live coordinator, or the replayed read time for a history rebuild.
        The record's interval is the 5-minute interval containing it.

        Prices are stored in $/kWh (no /1000 conversion).
        Runs in the main HA event loop (state reads are non-blocking).
        """
        now_utc = now_utc or datetime.now(tz=UTC)
        interval_start = interval_start_for(now_utc)

        # Core measurements
        net_import_kw: float = self._state_float(
            self._config.get(CONF_ENTITY_NET_IMPORT), fallback=0.0
        ) or 0.0

        solar_kw: float = max(
            0.0,
            self._state_float(
                self._config.get(CONF_ENTITY_SOLAR), fallback=0.0
            ) or 0.0,
        )

        total_load_kw: float = self._state_float(
            self._config.get(CONF_ENTITY_TOTAL_LOAD), fallback=0.0
        ) or 0.0

        # house_load_kw = total_load - sum(deferrable current_kw), clamped to 0
        # deferrable_loads is empty in v0.3, so house_load_kw = total_load (clamped)
        deferrable_load_kw: float = 0.0
        house_load_kw: float = max(0.0, total_load_kw - deferrable_load_kw)

        # Prices in $/kWh (no conversion)
        price_signal_seen: float = self._state_float(
            self._config.get(CONF_ENTITY_PRICE_SIGNAL), fallback=0.0
        ) or 0.0
        price_export_seen: float = self._state_float(
            self._config.get(CONF_ENTITY_PRICE_EXPORT), fallback=0.0
        ) or 0.0

        # Envelope limits
        envelope_import_limit_kw: float = self._state_float(
            self._config.get(CONF_ENTITY_ENVELOPE_IMPORT), fallback=5.0
        ) or 5.0
        envelope_export_limit_kw: float = abs(
            self._state_float(
                self._config.get(CONF_ENTITY_ENVELOPE_EXPORT), fallback=5.0
            ) or 5.0
        )

        # Shadow prices (all nullable, all in $/kWh).
        #
        # shadow_energy_price is the headline switchboard power-balance dual,
        # i.e. the marginal cost of one extra kWh of net energy at the meter.
        # The four constraint-specific shadows are non-zero only when that
        # particular constraint is binding for the current interval.
        shadow_energy_price = self._state_float_or_none(
            self._config.get(CONF_ENTITY_SHADOW_ENERGY)
        )
        if shadow_energy_price is not None:
            shadow_energy_price = round(shadow_energy_price, 6)

        shadow_load_forecast = self._state_float_or_none(
            self._config.get(CONF_ENTITY_SHADOW_LOAD_FORECAST)
        )
        shadow_solar_forecast = self._state_float_or_none(
            self._config.get(CONF_ENTITY_SHADOW_SOLAR_FORECAST)
        )
        shadow_envelope_import = self._state_float_or_none(
            self._config.get(CONF_ENTITY_SHADOW_ENVELOPE_IMPORT)
        )
        shadow_envelope_export = self._state_float_or_none(
            self._config.get(CONF_ENTITY_SHADOW_ENVELOPE_EXPORT)
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

        if flex_up_entity and self._get_state(flex_up_entity) is not None:
            flex_up = max(
                0.0,
                self._state_float(flex_up_entity, fallback=0.0) or 0.0,
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

        if flex_down_entity and self._get_state(flex_down_entity) is not None:
            flex_down = max(
                0.0,
                self._state_float(flex_down_entity, fallback=0.0) or 0.0,
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
            "interval_start_utc": format_interval(interval_start),
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