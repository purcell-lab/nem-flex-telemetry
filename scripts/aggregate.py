#!/usr/bin/env python3
"""NEM Flex Telemetry aggregation script.

Reads all household JSONL telemetry files from data/raw/,
validates them against the JSON Schema, deduplicates, and writes:
  - data/cohort/5min/YYYY/MM/DD.parquet
  - data/cohort/hourly/YYYY/MM/DD.parquet
  - data/cohort/daily/YYYY/MM/DD.parquet
  - site/data/cohort_flex_stack.json   (tab a: flex stack + price overlay)
  - site/data/price_response.json      (tab b: price-response scatter)
  - site/data/envelope_heatmap.json    (tab c: envelope compliance heatmap)
  - site/data/counterfactual.json      (tab d: cumulative savings ledger)
  - site/data/status.json              (dashboard header stats)

Usage (from repo root):
    python scripts/aggregate.py

Dependencies:
    pip install pandas pyarrow jsonschema

This script is also called by .github/workflows/aggregate.yml.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Try importing jsonschema; warn if unavailable
try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False
    logging.warning("jsonschema not installed. Schema validation will be skipped.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOG = logging.getLogger("aggregate")

# Repo root is one directory up from this script
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = REPO_ROOT / "data" / "raw"
DATA_COHORT = REPO_ROOT / "data" / "cohort"
SITE_DATA = REPO_ROOT / "site" / "data"
SCHEMA_FILE = REPO_ROOT / "schema" / "telemetry.schema.json"

# NEM regions
NEM_REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

# Required schema fields (in order)
REQUIRED_FIELDS = [
    "interval_start_utc",
    "region",
    "postcode_prefix",
    "net_import_kw",
    "price_signal_seen",
    "optimiser_setpoint_kw",
    "flex_available_up_kw",
    "flex_available_down_kw",
    "storage_soc_pct",
    "envelope_import_limit_kw",
    "envelope_export_limit_kw",
    "naive_baseline_kw",
]


# ---------------------------------------------------------------------------
# Schema loading and validation
# ---------------------------------------------------------------------------

def load_json_schema() -> dict[str, Any] | None:
    """Load the JSON Schema from disk."""
    if not SCHEMA_FILE.exists():
        _LOG.warning("Schema file not found at %s. Skipping validation.", SCHEMA_FILE)
        return None
    with SCHEMA_FILE.open() as f:
        return json.load(f)


def validate_record(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a single record against the JSON Schema.

    Returns a list of error messages (empty list means valid).
    """
    if not HAS_JSONSCHEMA:
        return []
    errors = []
    validator = jsonschema.Draft202012Validator(schema)
    for error in validator.iter_errors(record):
        errors.append(f"{'.'.join(str(p) for p in error.path) or 'root'}: {error.message}")
    return errors


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_jsonl() -> pd.DataFrame:
    """Walk data/raw/**/*.jsonl and load all records into a DataFrame.

    Invalid records are logged and skipped (not raised).
    """
    schema = load_json_schema()
    records: list[dict[str, Any]] = []
    valid_count = 0
    invalid_count = 0

    if not DATA_RAW.exists():
        _LOG.warning("data/raw/ directory does not exist. Returning empty DataFrame.")
        return pd.DataFrame(columns=REQUIRED_FIELDS)

    for jsonl_path in sorted(DATA_RAW.rglob("*.jsonl")):
        household_id = jsonl_path.parts[len(DATA_RAW.parts)]
        with jsonl_path.open() as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    _LOG.warning("%s line %d: JSON parse error: %s", jsonl_path, line_num, exc)
                    invalid_count += 1
                    continue

                if schema:
                    errors = validate_record(record, schema)
                    if errors:
                        _LOG.warning(
                            "%s line %d: schema validation failed: %s",
                            jsonl_path, line_num, "; ".join(errors)
                        )
                        invalid_count += 1
                        continue

                # Tag with household_id for traceability
                record["household_id"] = household_id
                records.append(record)
                valid_count += 1

    _LOG.info("Loaded %d valid records, skipped %d invalid records.", valid_count, invalid_count)

    if not records:
        return pd.DataFrame(columns=REQUIRED_FIELDS + ["household_id"])

    df = pd.DataFrame(records)
    # Parse interval timestamp
    df["interval_start_utc"] = pd.to_datetime(df["interval_start_utc"], utc=True)

    # Deduplicate: keep last record for each (household_id, interval_start_utc) pair
    before = len(df)
    df = df.sort_values("interval_start_utc").drop_duplicates(
        subset=["household_id", "interval_start_utc"], keep="last"
    )
    after = len(df)
    if before != after:
        _LOG.info("Deduplicated %d duplicate records.", before - after)

    return df


# ---------------------------------------------------------------------------
# Cohort parquet outputs
# ---------------------------------------------------------------------------

def write_parquet_by_date(df: pd.DataFrame, resolution: str) -> None:
    """Write cohort parquet files partitioned by date.

    Args:
        df: DataFrame with interval_start_utc as datetime64[utc].
        resolution: One of '5min', 'hourly', 'daily'.
    """
    if df.empty:
        _LOG.info("No data for %s parquet output.", resolution)
        return

    # Resample to the target resolution
    numeric_cols = [c for c in df.columns if c not in ("interval_start_utc", "region", "postcode_prefix", "household_id")]

    if resolution == "5min":
        resampled = df
    else:
        freq = "h" if resolution == "hourly" else "D"
        resampled = (
            df.set_index("interval_start_utc")
            .groupby(["household_id", "region", "postcode_prefix"])[numeric_cols]
            .resample(freq)
            .mean()
            .reset_index()
            .rename(columns={"interval_start_utc": "interval_start_utc"})
        )

    # Write one parquet file per date
    resampled["_date"] = resampled["interval_start_utc"].dt.date
    for date, group in resampled.groupby("_date"):
        year = date.year
        month = date.month
        day = date.day
        out_dir = DATA_COHORT / resolution / f"{year}" / f"{month:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{day:02d}.parquet"
        table = pa.Table.from_pandas(group.drop(columns=["_date"]))
        pq.write_table(table, out_path)
        _LOG.debug("Wrote %s", out_path)

    _LOG.info("Wrote %s parquet files for resolution=%s", resampled["_date"].nunique(), resolution)


# ---------------------------------------------------------------------------
# Dashboard view computations
# ---------------------------------------------------------------------------

def compute_flex_stack(df: pd.DataFrame) -> dict[str, Any]:
    """Tab a: Cohort flex stack over time.

    Returns hourly cohort totals of flex_available_up_kw, flex_available_down_kw,
    and mean price_signal_seen for the RRP overlay.
    """
    if df.empty:
        return {"intervals": [], "flex_up_kw": [], "flex_down_kw": [], "price_signal": []}

    hourly = (
        df.set_index("interval_start_utc")
        .resample("h")[["flex_available_up_kw", "flex_available_down_kw", "price_signal_seen"]]
        .agg({
            "flex_available_up_kw": "sum",
            "flex_available_down_kw": "sum",
            "price_signal_seen": "mean",
        })
        .reset_index()
    )

    return {
        "intervals": hourly["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
        "flex_up_kw": hourly["flex_available_up_kw"].round(2).tolist(),
        "flex_down_kw": hourly["flex_available_down_kw"].round(2).tolist(),
        "price_signal": hourly["price_signal_seen"].round(2).tolist(),
    }


def compute_price_response(df: pd.DataFrame) -> dict[str, Any]:
    """Tab b: Price-response scatter, faceted by region.

    Returns one entry per NEM region with arrays of (price, net_import) pairs.
    The relationship between these fields, across a cohort, yields the first
    open-data estimate of residential price elasticity in the NEM.
    """
    if df.empty:
        return {"regions": {r: {"price": [], "net_import": []} for r in NEM_REGIONS}}

    result: dict[str, Any] = {"regions": {}}
    for region in NEM_REGIONS:
        region_df = df[df["region"] == region]
        if region_df.empty:
            result["regions"][region] = {"price": [], "net_import": []}
            continue
        # Sample up to 5000 points per region to keep JSON manageable
        sample = region_df.sample(min(5000, len(region_df)), random_state=42)
        result["regions"][region] = {
            "price": sample["price_signal_seen"].round(2).tolist(),
            "net_import": sample["net_import_kw"].round(3).tolist(),
        }
    return result


def compute_envelope_heatmap(df: pd.DataFrame) -> dict[str, Any]:
    """Tab c: Envelope compliance heatmap (postcode_prefix x hour-of-day).

    Compliance is defined as: optimiser_setpoint_kw <= envelope_import_limit_kw
    and -optimiser_setpoint_kw <= envelope_export_limit_kw.
    Grid: postcode_prefix (rows) x hour-of-day 0-23 (columns).
    Cell value: compliance rate 0.0-1.0.
    """
    if df.empty:
        return {"postcode_prefixes": [], "hours": list(range(24)), "compliance": []}

    df = df.copy()
    df["hour"] = df["interval_start_utc"].dt.hour
    # Compliance flag: setpoint within envelope (both import and export)
    df["compliant"] = (
        (df["optimiser_setpoint_kw"] <= df["envelope_import_limit_kw"]) &
        (-df["optimiser_setpoint_kw"] <= df["envelope_export_limit_kw"])
    )

    pivot = (
        df.groupby(["postcode_prefix", "hour"])["compliant"]
        .mean()
        .unstack(fill_value=1.0)  # default to 1.0 (fully compliant) for missing cells
    )

    # Fill any missing hours
    for h in range(24):
        if h not in pivot.columns:
            pivot[h] = 1.0
    pivot = pivot[sorted(pivot.columns)]

    postcode_prefixes = pivot.index.tolist()
    compliance = pivot.round(3).values.tolist()

    return {
        "postcode_prefixes": postcode_prefixes,
        "hours": list(range(24)),
        "compliance": compliance,
    }


def compute_counterfactual(df: pd.DataFrame) -> dict[str, Any]:
    """Tab d: Counterfactual savings ledger.

    Formula per 5-min interval:
        savings_$ = (naive_baseline_kw - net_import_kw) * price_signal_seen / 1000 / 12

    The /1000 converts kW to MW (for $/MWh price units).
    The /12 converts the per-hour price to a 5-minute interval energy value.

    Returns:
        - intervals: list of timestamps
        - interval_savings: savings per interval ($)
        - cumulative_savings: running total ($)
        - total_savings: grand total ($)
    """
    if df.empty:
        return {
            "intervals": [],
            "interval_savings": [],
            "cumulative_savings": [],
            "total_savings": 0.0,
        }

    df = df.copy().sort_values("interval_start_utc")
    df["savings"] = (
        (df["naive_baseline_kw"] - df["net_import_kw"])
        * df["price_signal_seen"]
        / 1000.0
        / 12.0
    )

    # Aggregate to hourly for a cleaner chart (288 points/day is too many)
    hourly = (
        df.set_index("interval_start_utc")["savings"]
        .resample("h")
        .sum()
        .reset_index()
    )
    hourly["cumulative"] = hourly["savings"].cumsum()

    return {
        "intervals": hourly["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
        "interval_savings": hourly["savings"].round(4).tolist(),
        "cumulative_savings": hourly["cumulative"].round(4).tolist(),
        "total_savings": float(hourly["savings"].sum().round(2)),
    }


# ---------------------------------------------------------------------------
# Status JSON (for dashboard header and shields.io badges)
# ---------------------------------------------------------------------------

def compute_status(df: pd.DataFrame) -> dict[str, Any]:
    """Compute status metrics for the dashboard header and shields.io badges."""
    cohort_size = df["household_id"].nunique() if not df.empty else 0
    total_intervals = len(df)
    total_savings = 0.0
    if not df.empty:
        total_savings = float((
            (df["naive_baseline_kw"] - df["net_import_kw"])
            * df["price_signal_seen"]
            / 1000.0
            / 12.0
        ).sum().round(2))

    return {
        "cohort_size": cohort_size,
        "total_intervals": total_intervals,
        "total_savings_aud": total_savings,
        "last_updated": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regions": sorted(df["region"].unique().tolist()) if not df.empty else [],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full aggregation pipeline."""
    _LOG.info("Starting NEM Flex Telemetry aggregation.")
    _LOG.info("Repo root: %s", REPO_ROOT)

    # Load and validate all raw JSONL data
    df = load_all_jsonl()
    _LOG.info("Total records after deduplication: %d", len(df))

    # Write cohort parquet at three resolutions
    for resolution in ("5min", "hourly", "daily"):
        write_parquet_by_date(df, resolution)

    # Compute derived JSON views for the dashboard
    SITE_DATA.mkdir(parents=True, exist_ok=True)

    views = {
        "cohort_flex_stack.json": compute_flex_stack(df),
        "price_response.json": compute_price_response(df),
        "envelope_heatmap.json": compute_envelope_heatmap(df),
        "counterfactual.json": compute_counterfactual(df),
        "status.json": compute_status(df),
    }

    for filename, data in views.items():
        out_path = SITE_DATA / filename
        with out_path.open("w") as f:
            json.dump(data, f, indent=2)
        _LOG.info("Wrote %s", out_path)

    _LOG.info("Aggregation complete.")


if __name__ == "__main__":
    main()
