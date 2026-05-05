#!/usr/bin/env python3
"""NEM Flex Telemetry aggregation script.

Reads all household JSONL telemetry files from data/raw/,
validates them against the JSON Schema (v1.1), deduplicates, and writes:
  - data/cohort/5min/YYYY/MM/DD.parquet
  - data/cohort/hourly/YYYY/MM/DD.parquet
  - data/cohort/daily/YYYY/MM/DD.parquet
  - site/data/cohort_flex_stack.json   (tab a: flex stack + price overlay)
  - site/data/price_response.json      (tab b: price-response scatter)
  - site/data/envelope_heatmap.json    (tab c: envelope compliance heatmap)
  - site/data/counterfactual.json      (tab d: asymmetric counterfactual savings ledger)
  - site/data/buy_sell_spread.json     (tab e: buy/sell price spread by region)
  - site/data/status.json              (dashboard header stats)

Counterfactual formula (schema v1.1, asymmetric):
  if net_import_kw > 0 (household was net importing):
    savings = (naive_baseline_kw - net_import_kw) * price_signal_seen / 1000 / 12
  else (household was net exporting):
    savings = (naive_baseline_kw - net_import_kw) * price_export_seen / 1000 / 12

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

# Required schema fields (schema v1.1, 13 fields)
REQUIRED_FIELDS = [
    "interval_start_utc",
    "region",
    "postcode_prefix",
    "net_import_kw",
    "price_signal_seen",
    "price_export_seen",
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
    Records missing price_export_seen (schema v0.1.0 legacy) have that field
    backfilled with 0.0 so they can participate in aggregation with a note.
    """
    schema = load_json_schema()
    records: list[dict[str, Any]] = []
    valid_count = 0
    invalid_count = 0
    backfilled_count = 0

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

                # Backfill price_export_seen = 0.0 for v0.1.0 records
                if "price_export_seen" not in record:
                    record["price_export_seen"] = 0.0
                    backfilled_count += 1

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

    _LOG.info(
        "Loaded %d valid records, skipped %d invalid, backfilled %d legacy records.",
        valid_count, invalid_count, backfilled_count,
    )

    if not records:
        return pd.DataFrame(columns=REQUIRED_FIELDS + ["household_id"])

    df = pd.DataFrame(records)
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
    """Write cohort parquet files partitioned by date."""
    if df.empty:
        _LOG.info("No data for %s parquet output.", resolution)
        return

    numeric_cols = [
        c for c in df.columns
        if c not in ("interval_start_utc", "region", "postcode_prefix", "household_id")
    ]

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
    """Tab a: Cohort flex stack over time."""
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
    """Tab b: Price-response scatter, faceted by region."""
    if df.empty:
        return {"regions": {r: {"price": [], "net_import": []} for r in NEM_REGIONS}}

    result: dict[str, Any] = {"regions": {}}
    for region in NEM_REGIONS:
        region_df = df[df["region"] == region]
        if region_df.empty:
            result["regions"][region] = {"price": [], "net_import": []}
            continue
        sample = region_df.sample(min(5000, len(region_df)), random_state=42)
        result["regions"][region] = {
            "price": sample["price_signal_seen"].round(2).tolist(),
            "net_import": sample["net_import_kw"].round(3).tolist(),
        }
    return result


def compute_envelope_heatmap(df: pd.DataFrame) -> dict[str, Any]:
    """Tab c: Envelope compliance heatmap (postcode_prefix x hour-of-day)."""
    if df.empty:
        return {"postcode_prefixes": [], "hours": list(range(24)), "compliance": []}

    df = df.copy()
    df["hour"] = df["interval_start_utc"].dt.hour
    df["compliant"] = (
        (df["optimiser_setpoint_kw"] <= df["envelope_import_limit_kw"]) &
        (-df["optimiser_setpoint_kw"] <= df["envelope_export_limit_kw"])
    )

    pivot = (
        df.groupby(["postcode_prefix", "hour"])["compliant"]
        .mean()
        .unstack(fill_value=1.0)
    )

    for h in range(24):
        if h not in pivot.columns:
            pivot[h] = 1.0
    pivot = pivot[sorted(pivot.columns)]

    return {
        "postcode_prefixes": pivot.index.tolist(),
        "hours": list(range(24)),
        "compliance": pivot.round(3).values.tolist(),
    }


def compute_counterfactual(df: pd.DataFrame) -> dict[str, Any]:
    """Tab d: Asymmetric counterfactual savings ledger (schema v1.1).

    Formula per 5-min interval:
        If net_import_kw > 0 (net importer):
            savings = (naive_baseline_kw - net_import_kw) * price_signal_seen / 1000 / 12
        Else (net exporter):
            savings = (naive_baseline_kw - net_import_kw) * price_export_seen / 1000 / 12

    The /1000 converts kW to MW (for $/MWh price units).
    The /12 converts the per-hour price to a 5-minute interval energy value.

    Using price_export_seen for net-exporting intervals correctly accounts
    for periods when the feed-in tariff differs from the import price,
    including negative-FiT events where price_export_seen < 0.
    """
    if df.empty:
        return {
            "intervals": [],
            "interval_savings": [],
            "cumulative_savings": [],
            "total_savings": 0.0,
        }

    df = df.copy().sort_values("interval_start_utc")

    # Asymmetric price selection
    price_used = df["price_signal_seen"].where(df["net_import_kw"] > 0, df["price_export_seen"])

    df["savings"] = (
        (df["naive_baseline_kw"] - df["net_import_kw"])
        * price_used
        / 1000.0
        / 12.0
    )

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


def compute_buy_sell_spread(df: pd.DataFrame) -> dict[str, Any]:
    """Tab e: Buy/sell price spread by region.

    Returns hourly mean of price_signal_seen (buy) and price_export_seen (sell)
    per NEM region, flagging intervals where price_export_seen < 0 (negative FiT).

    This view makes the buy/sell asymmetry visible at cohort scale for the
    first time: as middle-of-day FiT collapses or goes negative, the spread
    widens and demand response becomes less valuable for exporters.
    """
    if df.empty:
        return {
            "regions": {
                r: {
                    "intervals": [],
                    "buy_price": [],
                    "sell_price": [],
                    "negative_fit_intervals": [],
                }
                for r in NEM_REGIONS
            }
        }

    result: dict[str, Any] = {"regions": {}}

    for region in NEM_REGIONS:
        region_df = df[df["region"] == region].copy()
        if region_df.empty:
            result["regions"][region] = {
                "intervals": [],
                "buy_price": [],
                "sell_price": [],
                "negative_fit_intervals": [],
            }
            continue

        hourly = (
            region_df.set_index("interval_start_utc")[
                ["price_signal_seen", "price_export_seen"]
            ]
            .resample("h")
            .mean()
            .reset_index()
        )

        # Negative FiT: intervals where hourly mean sell price < 0
        neg_fit_mask = hourly["price_export_seen"] < 0
        neg_fit_intervals = (
            hourly.loc[neg_fit_mask, "interval_start_utc"]
            .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            .tolist()
        )

        result["regions"][region] = {
            "intervals": hourly["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
            "buy_price": hourly["price_signal_seen"].round(2).tolist(),
            "sell_price": hourly["price_export_seen"].round(2).tolist(),
            "negative_fit_intervals": neg_fit_intervals,
        }

    return result


# ---------------------------------------------------------------------------
# Status JSON (for dashboard header and shields.io badges)
# ---------------------------------------------------------------------------

def compute_status(df: pd.DataFrame) -> dict[str, Any]:
    """Compute status metrics for the dashboard header and shields.io badges."""
    cohort_size = df["household_id"].nunique() if not df.empty else 0
    total_intervals = len(df)
    total_savings = 0.0

    if not df.empty:
        # Asymmetric counterfactual for status total
        price_used = df["price_signal_seen"].where(
            df["net_import_kw"] > 0, df["price_export_seen"]
        )
        total_savings = float((
            (df["naive_baseline_kw"] - df["net_import_kw"])
            * price_used
            / 1000.0
            / 12.0
        ).sum().round(2))

    return {
        "cohort_size": cohort_size,
        "total_intervals": total_intervals,
        "total_savings_aud": total_savings,
        "last_updated": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regions": sorted(df["region"].unique().tolist()) if not df.empty else [],
        "schema_version": "1.1",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full aggregation pipeline."""
    _LOG.info("Starting NEM Flex Telemetry aggregation (schema v1.1).")
    _LOG.info("Repo root: %s", REPO_ROOT)

    df = load_all_jsonl()
    _LOG.info("Total records after deduplication: %d", len(df))

    for resolution in ("5min", "hourly", "daily"):
        write_parquet_by_date(df, resolution)

    SITE_DATA.mkdir(parents=True, exist_ok=True)

    views = {
        "cohort_flex_stack.json": compute_flex_stack(df),
        "price_response.json": compute_price_response(df),
        "envelope_heatmap.json": compute_envelope_heatmap(df),
        "counterfactual.json": compute_counterfactual(df),
        "buy_sell_spread.json": compute_buy_sell_spread(df),
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
