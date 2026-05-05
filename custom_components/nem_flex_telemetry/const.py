"""Constants for the NEM Flex Telemetry integration."""

from __future__ import annotations

DOMAIN = "nem_flex_telemetry"
PLATFORMS: list[str] = ["sensor"]

VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# OAuth Device Flow constants
# ---------------------------------------------------------------------------
# Replace the placeholder below with the Client ID from your GitHub OAuth App.
# To find it: github.com/settings/applications -> your OAuth App -> Client ID.
OAUTH_CLIENT_ID = "Ov23liYGx66fvXkXA5Vs"
OAUTH_DEVICE_CODE_URL = "https://github.com/login/device/code"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
# public_repo is sufficient for writing to data/raw/<login>/** in a public repo.
# This is narrower than the full 'repo' scope.
OAUTH_SCOPE = "public_repo"
OAUTH_USER_AGENT = "nem-flex-telemetry/0.2.0"

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

# ---------------------------------------------------------------------------
# Entity mapping config keys
# ---------------------------------------------------------------------------
CONF_ENTITY_NET_IMPORT = "entity_net_import_kw"
CONF_ENTITY_PRICE_SIGNAL = "entity_price_signal_seen"
CONF_ENTITY_PRICE_EXPORT = "entity_price_export_seen"
CONF_ENTITY_SETPOINT = "entity_optimiser_setpoint_kw"
CONF_ENTITY_FLEX_UP = "entity_flex_available_up_kw"
CONF_ENTITY_FLEX_DOWN = "entity_flex_available_down_kw"
CONF_ENTITY_SOC = "entity_storage_soc_pct"
CONF_ENTITY_ENVELOPE_IMPORT = "entity_envelope_import_limit_kw"
CONF_ENTITY_ENVELOPE_EXPORT = "entity_envelope_export_limit_kw"
CONF_ENTITY_BASELINE = "entity_naive_baseline_kw"

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
SCHEMA_VERSION = "1.1"

# ---------------------------------------------------------------------------
# Battery flex defaults
# Used when HAEO does not expose flex_available_up/down directly.
# The coordinator derives headroom from battery max charge/discharge rate.
# Override by adding sensor.battery_max_charge_power / sensor.battery_max_discharge_power
# to your HA instance, or by setting these constants here before install.
# ---------------------------------------------------------------------------
DEFAULT_BATTERY_MAX_CHARGE_KW: float = 5.0
DEFAULT_BATTERY_MAX_DISCHARGE_KW: float = 5.0

# Entity IDs for battery max rate sensors (if available)
ENTITY_BATTERY_MAX_CHARGE = "sensor.battery_max_charge_power"
ENTITY_BATTERY_MAX_DISCHARGE = "sensor.battery_max_discharge_power"

# ---------------------------------------------------------------------------
# HAEO entity auto-discovery candidates
#
# Sourced from the user's live HAEO instance (ApexCharts dashboard cross-ref).
# These are canonical HAEO entity names as of HAEO v0.2.
#
# Structure per field:
#   primary    -- the preferred entity_id (None if not always present)
#   fallback   -- ordered list of alternatives to try if primary is absent
#   attribute  -- entity attribute to read (None = state value)
#   unit_hint  -- units to expect (informational, not enforced here)
#   notes      -- developer note for contributors
#
# Contributing default mappings for other optimisers (EMHASS, etc.) is
# welcome via PR to https://github.com/purcell-lab/nem-flex-telemetry.
# ---------------------------------------------------------------------------
DEFAULT_HAEO_ENTITIES: dict[str, dict] = {
    CONF_ENTITY_NET_IMPORT: {
        "primary": "sensor.grid_active_power",
        "fallback": ["sensor.haeo_grid_power", "sensor.power_consumption"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Realised grid import/export. Positive = importing, negative = exporting. "
            "Forecast attribute available on this entity for next-interval push (v0.3)."
        ),
    },
    CONF_ENTITY_PRICE_SIGNAL: {
        "primary": "number.grid_import_price",
        "fallback": ["sensor.amber_general_price", "sensor.localvolts_price", "sensor.haeo_current_price"],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": (
            "Buy price seen by the optimiser. "
            "Convert to $/MWh on push (multiply by 1000)."
        ),
    },
    CONF_ENTITY_PRICE_EXPORT: {
        "primary": "number.grid_export_price",
        "fallback": ["sensor.amber_feed_in_price", "sensor.localvolts_export_price"],
        "attribute": None,
        "unit_hint": "$/kWh",
        "notes": (
            "Sell price seen by the optimiser. Convert to $/MWh on push. "
            "Sign: positive = paid for exports, negative = paying to export (negative-FiT event)."
        ),
    },
    CONF_ENTITY_SETPOINT: {
        "primary": "sensor.battery_active_power",
        "fallback": ["sensor.haeo_battery_setpoint", "sensor.haeo_optimiser_setpoint"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Battery active power as the dominant flex setpoint. "
            "Forecast attribute is the LP plan for next-interval push (v0.3)."
        ),
    },
    CONF_ENTITY_FLEX_UP: {
        "primary": None,
        "fallback": ["sensor.haeo_flex_up", "sensor.haeo_available_charge_power", "sensor.haeo_headroom_up"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Headroom to increase load. "
            "If not present on the HAEO instance, derived in coordinator from "
            "battery max charge minus current setpoint, plus any deferrable load capacity."
        ),
    },
    CONF_ENTITY_FLEX_DOWN: {
        "primary": None,
        "fallback": ["sensor.haeo_flex_down", "sensor.haeo_available_discharge_power", "sensor.haeo_headroom_down"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Headroom to decrease load. "
            "If not present, derived from battery max discharge minus current setpoint."
        ),
    },
    CONF_ENTITY_SOC: {
        "primary": "sensor.battery_state_of_charge",
        "fallback": ["sensor.battery_soc", "sensor.home_battery_state_of_charge"],
        "attribute": None,
        "unit_hint": "%",
        "notes": "Home battery state of charge (0-100).",
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
            "The user's instance shows this signed negative for plotting; "
            "we store as a positive kW limit."
        ),
    },
    CONF_ENTITY_BASELINE: {
        "primary": "sensor.load_power",
        "fallback": ["sensor.haeo_baseline_power", "sensor.haeo_naive_baseline", "sensor.haeo_counterfactual_power"],
        "attribute": None,
        "unit_hint": "kW",
        "notes": (
            "Realised load as a proxy baseline. "
            "For a true counterfactual, the LP computes a 'no flex' branch; "
            "if HAEO exposes sensor.haeo_naive_baseline use that. "
            "Otherwise sensor.load_power is the conservative estimate."
        ),
    },
}

# ---------------------------------------------------------------------------
# Context entities: reference-only, not pushed in v0.2.
# Discovered and logged at startup for forecast-horizon publishing in v0.3.
# ---------------------------------------------------------------------------
CONTEXT_ENTITIES: dict[str, dict] = {
    "pv_forecast": {
        "primary": "number.solar_forecast",
        "fallback": ["sensor.solar_forecast_power"],
        "notes": "Solar forecast (kW). HAEO publishes forecast attribute for next 7 days.",
    },
    "load_forecast": {
        "primary": "sensor.load_power",
        "fallback": ["sensor.haeo_load_forecast"],
        "notes": "Load forecast (kW) via forecast attribute.",
    },
    # Regional PD7day forecast: auto-selected by region in discovery.py
    "regional_price_forecast": {
        "primary": "sensor.qld1_pd7day_forecast",
        "fallback": [
            "sensor.nsw1_pd7day_forecast",
            "sensor.vic1_pd7day_forecast",
            "sensor.sa1_pd7day_forecast",
            "sensor.tas1_pd7day_forecast",
        ],
        "notes": (
            "AEMO PD 7-day regional price forecast. "
            "Region-specific: auto-selected from the region set in the identity step."
        ),
    },
    "ev1_soc": {
        "primary": "sensor.ev1_state_of_charge",
        "fallback": [],
        "notes": "EV1 SOC. Optional context for V2G flex inference.",
    },
    "ev2_soc": {
        "primary": "sensor.ev2_state_of_charge",
        "fallback": [],
        "notes": "EV2 SOC. Optional context for V2G flex inference.",
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
# None for fields where primary is not always present (flex headroom).
# ---------------------------------------------------------------------------
DEFAULT_ENTITY_MAPPINGS: dict[str, str] = {
    key: spec["primary"] or (spec["fallback"][0] if spec["fallback"] else "")
    for key, spec in DEFAULT_HAEO_ENTITIES.items()
}
