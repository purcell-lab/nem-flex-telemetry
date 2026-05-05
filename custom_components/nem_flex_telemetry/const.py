"""Constants for the NEM Flex Telemetry integration."""

from __future__ import annotations

DOMAIN = "nem_flex_telemetry"
PLATFORMS: list[str] = ["sensor"]

# Config entry keys
CONF_GITHUB_PAT = "github_pat"
CONF_HOUSEHOLD_ID = "household_id"
CONF_POSTCODE_PREFIX = "postcode_prefix"
CONF_REGION = "region"
CONF_OPT_IN_COHORT = "opt_in_cohort"
CONF_LICENCE_AGREED = "licence_agreed"

# Entity mapping config keys
CONF_ENTITY_NET_IMPORT = "entity_net_import_kw"
CONF_ENTITY_PRICE_SIGNAL = "entity_price_signal_seen"
CONF_ENTITY_SETPOINT = "entity_optimiser_setpoint_kw"
CONF_ENTITY_FLEX_UP = "entity_flex_available_up_kw"
CONF_ENTITY_FLEX_DOWN = "entity_flex_available_down_kw"
CONF_ENTITY_SOC = "entity_storage_soc_pct"
CONF_ENTITY_ENVELOPE_IMPORT = "entity_envelope_import_limit_kw"
CONF_ENTITY_ENVELOPE_EXPORT = "entity_envelope_export_limit_kw"
CONF_ENTITY_BASELINE = "entity_naive_baseline_kw"

# NEM region codes
NEM_REGIONS: list[str] = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

# Update interval: 5 minutes in seconds
UPDATE_INTERVAL_SECONDS = 300

# Push interval: 1 hour (buffer 12 records per push to minimise API calls)
PUSH_INTERVAL_SECONDS = 3600
RECORDS_PER_PUSH = 12  # 12 x 5-min intervals = 1 hour

# GitHub repo details
GITHUB_REPO = "purcell-lab/nem-flex-telemetry"
DATA_RAW_PATH_TEMPLATE = "data/raw/{household_id}/{year}/{month:02d}/{day:02d}.jsonl"

# Sensor unique ID suffixes
SENSOR_LAST_PUSH_TIME = "last_push_time"
SENSOR_RECORDS_PUSHED_TODAY = "records_pushed_today"
SENSOR_PUSH_ERRORS = "push_errors"
SENSOR_COHORT_SIZE = "cohort_size"

# Service names
SERVICE_MANUAL_PUSH = "manual_push"

# Schema version
SCHEMA_VERSION = "0.1.0"

# Default HAEO entity IDs (users should override these in config flow)
DEFAULT_ENTITY_MAPPINGS: dict[str, str] = {
    CONF_ENTITY_NET_IMPORT: "sensor.haeo_net_import_power",
    CONF_ENTITY_PRICE_SIGNAL: "sensor.haeo_price_signal",
    CONF_ENTITY_SETPOINT: "sensor.haeo_optimiser_setpoint",
    CONF_ENTITY_FLEX_UP: "sensor.haeo_flex_available_up",
    CONF_ENTITY_FLEX_DOWN: "sensor.haeo_flex_available_down",
    CONF_ENTITY_SOC: "sensor.haeo_battery_soc",
    CONF_ENTITY_ENVELOPE_IMPORT: "sensor.haeo_envelope_import_limit",
    CONF_ENTITY_ENVELOPE_EXPORT: "sensor.haeo_envelope_export_limit",
    CONF_ENTITY_BASELINE: "sensor.haeo_naive_baseline",
}
