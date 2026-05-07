"""Constants for the NEM Flex Telemetry integration."""

from __future__ import annotations

import re

DOMAIN = "nem_flex_telemetry"
PLATFORMS: list[str] = ["sensor"]

VERSION = "0.4.0"

# ---------------------------------------------------------------------------
# OAuth Device Flow constants
# ---------------------------------------------------------------------------
OAUTH_CLIENT_ID = "Ov23liYGx66fvXkXA5Vs"
OAUTH_DEVICE_CODE_URL = "https://github.com/login/device/code"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
OAUTH_SCOPE = "public_repo"
OAUTH_USER_AGENT = "nem-flex-telemetry/0.3.0"

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------
CONF_TOKEN = "oauth_token"
CONF_GITHUB_LOGIN = "github_login"
CONF_HOUSEHOLD_ID = "household_id"
CONF_POSTCODE_PREFIX = "postcode_prefix"
CONF_REGION = "region"
CONF_OPT_IN_COHORT = "opt_in_cohort"
CONF_LICENCE_AGREED = "licence_agreed"
CONF_CONSENT_TIMESTAMP = "consent_timestamp"

# Asset capacity config keys (asked in async_step_assets)
CONF_HOME_BATTERY_CAPACITY_KWH = "home_battery_capacity_kwh"
CONF_EV1_CAPACITY_KWH = "ev1_capacity_kwh"
CONF_EV2_CAPACITY_KWH = "ev2_capacity_kwh"

# ---------------------------------------------------------------------------
# Entity mapping config keys (top-level / HAEO)
# ---------------------------------------------------------------------------
CONF_ENTITY_NET_IMPORT = "entity_net_import_kw"
CONF_ENTITY_SOLAR = "entity_solar_kw"
CONF_ENTITY_TOTAL_LOAD = "entity_total_load_kw"
CONF_ENTITY_PRICE_SIGNAL = "entity_price_signal_seen"
CONF_ENTITY_PRICE_EXPORT = "entity_price_export_seen"
CONF_ENTITY_ENVELOPE_IMPORT = "entity_envelope_import_limit_kw"
CONF_ENTITY_ENVELOPE_EXPORT = "entity_envelope_export_limit_kw"
CONF_ENTITY_FLEX_UP = "entity_flex_available_up_kw"
CONF_ENTITY_FLEX_DOWN = "entity_flex_available_down_kw"

# Shadow price entity config keys
CONF_ENTITY_SHADOW_ENERGY = "entity_shadow_energy_price"
CONF_ENTITY_SHADOW_LOAD_FORECAST = "entity_shadow_load_forecast_price"
CONF_ENTITY_SHADOW_SOLAR_FORECAST = "entity_shadow_solar_forecast_price"
CONF_ENTITY_SHADOW_ENVELOPE_IMPORT = "entity_shadow_envelope_import_price"
CONF_ENTITY_SHADOW_ENVELOPE_EXPORT = "entity_shadow_envelope_export_price"

# ---------------------------------------------------------------------------
# NEM region codes
# ---------------------------------------------------------------------------
NEM_REGIONS: list[str] = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------
UPDATE_INTERVAL_SECONDS = 300   # 5-minute poll interval
PUSH_INTERVAL_SECONDS = 3600    # 1-hour push interval
RECORDS_PER_PUSH = 12           # 12 x 5-min intervals = 1 hour

# ---------------------------------------------------------------------------
# GitHub repo details
# ---------------------------------------------------------------------------
GITHUB_REPO = "purcell-lab/nem-flex-telemetry"
DATA_RAW_PATH_TEMPLATE = "data/raw/{household_id}/{year}/{month:02d}/{day:02d}.jsonl"

# ---------------------------------------------------------------------------
# Sensor unique ID suffixes
# ---------------------------------------------------------------------------
SENSOR_LAST_PUSH_TIME = "last_push_time"
SENSOR_RECORDS_PUSHED_TODAY = "records_pushed_today"
SENSOR_PUSH_ERRORS = "push_errors"
SENSOR_COHORT_SIZE = "cohort_size"

# ---------------------------------------------------------------------------
# Service names
# ---------------------------------------------------------------------------
SERVICE_MANUAL_PUSH = "manual_push"

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "2.0"

# ---------------------------------------------------------------------------
# Battery and EV flex defaults (kW).
# Used for asset records and flex headroom derivation when HAEO does not
# expose charge/discharge rate entities directly.
# Configurable per-asset in v0.4 via options flow.
# ---------------------------------------------------------------------------
DEFAULT_BATTERY_MAX_CHARGE_KW: float = 5.0
DEFAULT_BATTERY_MAX_DISCHARGE_KW: float = 5.0
DEFAULT_EV_MAX_CHARGE_KW: float = 7.4
DEFAULT_EV_MAX_DISCHARGE_KW: float = 7.4

# Hybrid inverter and DCEV charger fallbacks (kW). Used when the corresponding
# number.* entity is missing. Sized for Mark Purcell's reference stack:
# 30 kW hybrid inverter (PV + battery), 25 kW DC bidirectional EV charger.
DEFAULT_INVERTER_AC_TO_DC_KW: float = 30.0
DEFAULT_INVERTER_DC_TO_AC_KW: float = 30.0
DEFAULT_DCEV_AC_TO_DC_KW: float = 25.0
DEFAULT_DCEV_DC_TO_AC_KW: float = 25.0

# Entity IDs for power-rating number entities. The publisher reads each on every
# 5-minute push; missing/unavailable entities fall back to the DEFAULT_* above.
# Domain is `number.` (user-writable) per HAEO/EMHASS convention.
ENTITY_BATTERY_MAX_CHARGE = "number.battery_max_charge_power"
ENTITY_BATTERY_MAX_DISCHARGE = "number.battery_max_discharge_power"
ENTITY_INVERTER_AC_TO_DC = "number.inverter_max_ac_to_dc_power"
ENTITY_INVERTER_DC_TO_AC = "number.inverter_max_dc_to_ac_power"
ENTITY_DCEV_AC_TO_DC = "number.dcev_inverter_max_ac_to_dc_power"
ENTITY_DCEV_DC_TO_AC = "number.dcev_inverter_max_dc_to_ac_power"

# ---------------------------------------------------------------------------
# GLOBAL_SWEEP_PATTERNS
#
# Compiled regex patterns used in discovery.py to sweep hass.states.async_all()
# for HAEO entities not already mapped via DEFAULT_HAEO_ENTITIES or ASSET_DEFAULTS.
# Run at config-flow time and again at every coordinator startup (reload).
#
# Any entity matching these patterns that is not already in the named-entity
# mapping is surfaced in 'unmapped_entities' for the user to manually associate.
# ---------------------------------------------------------------------------
GLOBAL_SWEEP_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^sensor\..*_shadow_price$"),
    re.compile(r"^sensor\..*_state_of_charge$"),
    re.compile(r"^sensor\..*_active_power$"),
    re.compile(r"^number\.grid_.*$"),
    re.compile(r"^binary_sensor\..*_(plugged|charging|connected)$"),
)

# ---------------------------------------------------------------------------
# HAEO entity auto-discovery candidates (schema v2.0)
#
# Prices stay in $/kWh as HAEO emits them (no /1000 conversion).
# ---------------------------------------------------------------------------
DEFAULT_HAEO_ENTITIES: dict[str, dict] = {
    CONF_ENTITY_NET_IMPORT: {
        "primary": "sensor.grid_active_power",
        "fallback": ["sensor.haeo_grid_power", "sensor.power_consumption"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Realised grid import/export. Positive = importing, negative = exporting."
        ),
    },
    CONF_ENTITY_SOLAR: {
        "primary": "number.solar_forecast",
        "fallback": ["sensor.solar_forecast_power", "sensor.pv_power"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Solar generation (kW, always >= 0). Uses current state value, not forecast attribute."
        ),
    },
    CONF_ENTITY_TOTAL_LOAD: {
        "primary": "sensor.load_power",
        "fallback": ["sensor.haeo_load_power", "sensor.total_load_power"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Total household load (kW). Used internally to derive house_load_kw. "
            "house_load_kw = total_load - sum(deferrable current_kw), clamped to 0."
        ),
    },
    CONF_ENTITY_PRICE_SIGNAL: {
        "primary": "number.grid_import_price",
        "fallback": ["sensor.amber_general_price", "sensor.localvolts_price", "sensor.haeo_current_price"],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": (
            "Buy price seen by the optimiser. Stored in $/kWh (no conversion)."
        ),
    },
    CONF_ENTITY_PRICE_EXPORT: {
        "primary": "number.grid_export_price",
        "fallback": ["sensor.amber_feed_in_price", "sensor.localvolts_export_price"],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": (
            "Sell price seen by the optimiser. Stored in $/kWh. "
            "Positive = paid for exports; negative = negative-FiT event."
        ),
    },
    CONF_ENTITY_ENVELOPE_IMPORT: {
        "primary": "number.grid_import_limit",
        "fallback": ["sensor.csip_aus_import_limit", "sensor.doe_import_limit", "sensor.dynamic_import_limit"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": "DNSP import envelope via CSIP-AUS or static limit. Positive kW magnitude.",
    },
    CONF_ENTITY_ENVELOPE_EXPORT: {
        "primary": "number.grid_export_limit",
        "fallback": ["sensor.csip_aus_export_limit", "sensor.doe_export_limit", "sensor.dynamic_export_limit"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "DNSP export envelope via CSIP-AUS or static limit. "
            "Stored as positive kW magnitude."
        ),
    },
    # NOTE: flex_available_up_kw and flex_available_down_kw are NOT discoverable.
    # They are derived in the coordinator from battery / EV max charge / max
    # discharge ratings, current setpoints, SOC, and envelope limits. Surfacing
    # them in the discovery flow would only confuse users by asking them to
    # map sensors that do not exist in HAEO. The CONF_ENTITY_FLEX_UP /
    # CONF_ENTITY_FLEX_DOWN keys are retained in const.py as optional
    # power-user overrides via YAML / options, but are intentionally absent
    # from this auto-discovery dict.
    # Shadow price entities
    CONF_ENTITY_SHADOW_ENERGY: {
        "primary": "sensor.switchboard_power_balance_shadow_price",
        "fallback": [],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": (
            "HAEO LP dual on the whole-of-house energy balance constraint at "
            "the switchboard. This is the marginal cost of one extra kWh of "
            "net energy at the meter for the current dispatch interval."
        ),
    },
    CONF_ENTITY_SHADOW_LOAD_FORECAST: {
        "primary": "sensor.load_forecast_limit_shadow_price",
        "fallback": [],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": "HAEO LP dual for the load forecast constraint.",
    },
    CONF_ENTITY_SHADOW_SOLAR_FORECAST: {
        "primary": "sensor.solar_forecast_limit_shadow_price",
        "fallback": [],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": "HAEO LP dual for the solar forecast constraint.",
    },
    CONF_ENTITY_SHADOW_ENVELOPE_IMPORT: {
        "primary": "sensor.grid_max_import_power_shadow_price",
        "fallback": [],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": "HAEO LP dual for the grid import envelope constraint.",
    },
    CONF_ENTITY_SHADOW_ENVELOPE_EXPORT: {
        "primary": "sensor.grid_max_export_power_shadow_price",
        "fallback": [],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": "HAEO LP dual for the grid export envelope constraint.",
    },
}

# ---------------------------------------------------------------------------
# ASSET_DEFAULTS
#
# Per-asset entity mappings for Mark Purcell's install.
# Each asset has kind, bidirectional_capable, and entity sources.
# EV assets include connection_state inference metadata.
# ---------------------------------------------------------------------------
ASSET_DEFAULTS: dict[str, dict] = {
    "home_battery": {
        "kind": "stationary_battery",
        "bidirectional_capable": True,
        "soc_entity": "sensor.battery_state_of_charge",
        "setpoint_entity": "sensor.battery_active_power",
        "shadow_entity": "sensor.battery_power_balance_shadow_price",
        "capacity_kwh": 13.5,
        "max_charge_kw": DEFAULT_BATTERY_MAX_CHARGE_KW,
        "max_discharge_kw": DEFAULT_BATTERY_MAX_DISCHARGE_KW,
    },
    "ev1": {
        "kind": "ev",
        "bidirectional_capable": True,
        "soc_entity": "sensor.ev1_state_of_charge",
        "setpoint_entity": "sensor.ev1_active_power",
        "shadow_entity": "sensor.ev1_power_balance_shadow_price",
        "capacity_kwh": 75.0,
        "max_charge_kw": DEFAULT_EV_MAX_CHARGE_KW,
        "max_discharge_kw": DEFAULT_EV_MAX_DISCHARGE_KW,
    },
    "ev2": {
        "kind": "ev",
        "bidirectional_capable": True,
        "soc_entity": "sensor.ev2_state_of_charge",
        "setpoint_entity": "sensor.ev2_active_power",
        "shadow_entity": "sensor.ev2_power_balance_shadow_price",
        "capacity_kwh": 60.0,
        "max_charge_kw": DEFAULT_EV_MAX_CHARGE_KW,
        "max_discharge_kw": DEFAULT_EV_MAX_DISCHARGE_KW,
    },
}

# ---------------------------------------------------------------------------
# Context entities: reference-only, not pushed in v0.3.
# ---------------------------------------------------------------------------
CONTEXT_ENTITIES: dict[str, dict] = {
    "regional_price_forecast": {
        "primary": "sensor.qld1_pd7day_forecast",
        "fallback": [
            "sensor.nsw1_pd7day_forecast",
            "sensor.vic1_pd7day_forecast",
            "sensor.sa1_pd7day_forecast",
            "sensor.tas1_pd7day_forecast",
        ],
        "notes": "AEMO PD 7-day regional price forecast. Region-specific: auto-selected from the region set in the identity step.",
    },
}

# Map NEM region code to canonical PD7day forecast entity name
REGION_PD7DAY_ENTITY: dict[str, str] = {
    "NSW1": "sensor.nsw1_pd7day_forecast",
    "QLD1": "sensor.qld1_pd7day_forecast",
    "VIC1": "sensor.vic1_pd7day_forecast",
    "SA1":  "sensor.sa1_pd7day_forecast",
    "TAS1": "sensor.tas1_pd7day_forecast",
}

# ---------------------------------------------------------------------------
# DEFAULT_ENTITY_MAPPINGS: primary candidate per field for UI defaults.
# ---------------------------------------------------------------------------
DEFAULT_ENTITY_MAPPINGS: dict[str, str] = {
    key: spec["primary"] or (spec["fallback"][0] if spec["fallback"] else "")
    for key, spec in DEFAULT_HAEO_ENTITIES.items()
}
