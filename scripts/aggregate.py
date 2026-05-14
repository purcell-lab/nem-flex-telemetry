#!/usr/bin/env python3
"""NEM Flex Telemetry aggregation script.

Reads all household JSONL telemetry files from data/raw/,
validates them against the JSON Schema (v2.0), deduplicates, and writes:
  - data/cohort/5min/YYYY/MM/DD.parquet
  - data/cohort/hourly/YYYY/MM/DD.parquet
  - data/cohort/daily/YYYY/MM/DD.parquet
  - site/data/cohort_flex_stack.json   (tab 1: flex stack + price overlay)
  - site/data/price_response.json      (tab 2: dual price-response curves: import vs buy, export vs sell)
  - site/data/curtailment_heatmap.json (tab 3: export curtailment vs static cap, kWh and $ by hour)
  - site/data/counterfactual.json      (tab 4: asymmetric counterfactual savings ledger)
  - site/data/buy_sell_spread.json     (tab 5: buy/sell price spread by region)
  - site/data/assets_summary.json      (tab 6: asset mix, V2G duty cycle, dispatch share)
  - site/data/shadow_prices.json       (tab 7: shadow price distribution and envelope heatmap)
  - site/data/status.json              (dashboard header stats)

Counterfactual formula (schema v2.0, $/kWh throughout):
  effective_price_kwh = price_signal_seen if net_import_kw > 0 else price_export_seen
  saving_aud = (naive_baseline_kw - net_import_kw) * effective_price_kwh * (interval_seconds / 3600)

  where interval_seconds = 300 (5 minutes).

All prices are in $/kWh. No /1000 scaling is applied.

Usage (from repo root):
    python scripts/aggregate.py

Dependencies:
    pip install pandas pyarrow jsonschema
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = REPO_ROOT / "data" / "raw"
DATA_COHORT = REPO_ROOT / "data" / "cohort"
SITE_DATA = REPO_ROOT / "site" / "data"
SCHEMA_FILE = REPO_ROOT / "schema" / "telemetry.schema.json"

NEM_REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]

# 5-minute interval in seconds
INTERVAL_SECONDS = 300

# Publisher household-ID aliasing.
#
# When a publisher re-registers under a new UUID (e.g. after a HA reinstall or
# a token rotation) the same physical household ends up with two directories
# under data/raw/. The cohort_size stat (and any per-household groupby) would
# then double-count it. This map collapses known aliases to their canonical ID.
# Add new entries here whenever you intend two raw IDs to be treated as one site.
HOUSEHOLD_ALIAS: dict[str, str] = {
    # Original test ID -> Mark's canonical UUID (Sunshine Coast QLD).
    "123": "cd01946f-3770-406e-8936-2c7d039e1b4c",
}

# Required top-level schema fields (schema v2.0, 18 flat fields + arrays)
REQUIRED_FIELDS = [
    "schema_version",
    "interval_start_utc",
    "region",
    "postcode_prefix",
    "net_import_kw",
    "solar_kw",
    "house_load_kw",
    "deferrable_load_kw",
    "naive_baseline_kw",
    "naive_baseline_method",
    "price_signal_seen",
    "price_export_seen",
    "envelope_import_limit_kw",
    "envelope_export_limit_kw",
    "flex_available_up_kw",
    "flex_available_down_kw",
    "shadow_energy_price",
    "shadow_load_forecast_price",
    "shadow_solar_forecast_price",
    "shadow_envelope_import_price",
    "shadow_envelope_export_price",
    "assets",
    "deferrable_loads",
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
    """Validate a single record against the JSON Schema."""
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
    """Walk data/raw/**/*.jsonl and load all v2.0 records into a DataFrame.

    Invalid records and v1.x records are logged and skipped.
    """
    schema = load_json_schema()
    records: list[dict[str, Any]] = []
    valid_count = 0
    invalid_count = 0
    skipped_version_count = 0

    if not DATA_RAW.exists():
        _LOG.warning("data/raw/ directory does not exist. Returning empty DataFrame.")
        return pd.DataFrame(columns=REQUIRED_FIELDS)

    # Track aliases that fired this run so we log each remap once (first hit) rather
    # than per-file. Surfaces unintended aliasing without spamming the log.
    seen_aliases: set[str] = set()

    for jsonl_path in sorted(DATA_RAW.rglob("*.jsonl")):
        raw_household_id = jsonl_path.parts[len(DATA_RAW.parts)]
        # Collapse known publisher-side ID changes for the same physical site so the
        # cohort_size stat reflects unique households, not unique POST URLs. New entries
        # belong here whenever a publisher re-registers under a fresh UUID.
        household_id = HOUSEHOLD_ALIAS.get(raw_household_id, raw_household_id)
        if household_id != raw_household_id and raw_household_id not in seen_aliases:
            _LOG.info(
                "Aliasing household %r -> %r (HOUSEHOLD_ALIAS map)",
                raw_household_id,
                household_id,
            )
            seen_aliases.add(raw_household_id)
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

                # Reject v1.x records (clean break, no migration)
                if record.get("schema_version", "1.1") != "2.0":
                    _LOG.debug(
                        "%s line %d: skipping record with schema_version=%r (v2.0 required)",
                        jsonl_path,
                        line_num,
                        record.get("schema_version"),
                    )
                    skipped_version_count += 1
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

                record["household_id"] = household_id
                records.append(record)
                valid_count += 1

    _LOG.info(
        "Loaded %d valid records, skipped %d invalid, skipped %d legacy version records.",
        valid_count, invalid_count, skipped_version_count,
    )

    if not records:
        return pd.DataFrame(columns=REQUIRED_FIELDS + ["household_id"])

    df = pd.DataFrame(records)
    df["interval_start_utc"] = pd.to_datetime(df["interval_start_utc"], utc=True)

    before = len(df)
    df = df.sort_values("interval_start_utc").drop_duplicates(
        subset=["household_id", "interval_start_utc"], keep="last"
    )
    after = len(df)
    if before != after:
        _LOG.info("Deduplicated %d duplicate records.", before - after)

    return df


# ---------------------------------------------------------------------------
# Asset record expansion
# ---------------------------------------------------------------------------

def expand_assets(df: pd.DataFrame) -> pd.DataFrame:
    """Explode the assets[] array into a separate long-format DataFrame.

    Returns a DataFrame with columns:
        interval_start_utc, household_id, region, postcode_prefix,
        asset_id, kind, bidirectional_capable, capacity_kwh,
        soc_pct, setpoint_kw, available_up_kw, available_down_kw,
        shadow_power_balance_price, connection_state (nullable),
        power_flow_capability (nullable)
    """
    if df.empty or "assets" not in df.columns:
        return pd.DataFrame()

    rows = []
    for _, rec in df.iterrows():
        assets = rec.get("assets", [])
        if not isinstance(assets, list):
            continue
        for asset in assets:
            rows.append({
                "interval_start_utc": rec["interval_start_utc"],
                "household_id": rec["household_id"],
                "region": rec["region"],
                "postcode_prefix": rec["postcode_prefix"],
                "asset_id": asset.get("asset_id", ""),
                "kind": asset.get("kind", ""),
                "bidirectional_capable": asset.get("bidirectional_capable", False),
                "capacity_kwh": asset.get("capacity_kwh", 0.0),
                "soc_pct": asset.get("soc_pct", 0.0),
                "setpoint_kw": asset.get("setpoint_kw"),
                "available_up_kw": asset.get("available_up_kw", 0.0),
                "available_down_kw": asset.get("available_down_kw", 0.0),
                "shadow_power_balance_price": asset.get("shadow_power_balance_price"),
                "connection_state": asset.get("connection_state"),
                "power_flow_capability": asset.get("power_flow_capability"),
            })

    if not rows:
        return pd.DataFrame()

    assets_df = pd.DataFrame(rows)
    assets_df["interval_start_utc"] = pd.to_datetime(
        assets_df["interval_start_utc"], utc=True
    )
    return assets_df


# ---------------------------------------------------------------------------
# Cohort parquet outputs
# ---------------------------------------------------------------------------

def write_parquet_by_date(df: pd.DataFrame, resolution: str) -> None:
    """Write cohort parquet files partitioned by date."""
    if df.empty:
        _LOG.info("No data for %s parquet output.", resolution)
        return

    # Columns safe to resample numerically (exclude arrays and strings)
    numeric_cols = [
        c for c in df.columns
        if c not in (
            "interval_start_utc", "region", "postcode_prefix", "household_id",
            "naive_baseline_method", "schema_version", "assets", "deferrable_loads",
        )
    ]

    if resolution == "5min":
        resampled = df.copy()
    else:
        freq = "h" if resolution == "hourly" else "D"
        resampled = (
            df.set_index("interval_start_utc")
            .groupby(["household_id", "region", "postcode_prefix"])[numeric_cols]
            .resample(freq)
            .mean()
            .reset_index()
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

    _LOG.info("Wrote %s parquet files for resolution=%s", resampled["_date"].nunique(), resolution)


# ---------------------------------------------------------------------------
# Dashboard view computations (tabs 1-5: updated for $/kWh)
# ---------------------------------------------------------------------------

def compute_flex_stack(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 1: Cohort flex stack over time ($/kWh dual-price overlay).

    Cohort flex at time T = sum across households of flex_available_(up|down)_kw
    at that 5-minute interval. Hourly = time-mean of those 5-minute cohort sums
    (NOT a sum across the 12 sub-intervals, which would 12x-inflate the kW).

    Two price overlays drive the flex behaviours separately:
      - buy_price  (price_signal_seen): drives import / flex_up
      - sell_price (price_export_seen): drives export / flex_down
    Both are reported as cohort means then time-averaged over the hour.

    'price_signal' is retained for backward compatibility with older clients;
    it equals buy_price.
    """
    if df.empty:
        return {
            "intervals": [], "flex_up_kw": [], "flex_down_kw": [],
            "buy_price": [], "sell_price": [], "price_signal": [],
            "price_unit": "$/kWh",
        }

    # Step 1: cohort sum at each 5-minute interval
    cohort_5min = (
        df.groupby("interval_start_utc")
        .agg(
            flex_up_kw=("flex_available_up_kw", "sum"),
            flex_down_kw=("flex_available_down_kw", "sum"),
            buy_price=("price_signal_seen", "mean"),
            sell_price=("price_export_seen", "mean"),
        )
        .reset_index()
    )

    # Step 2: time-mean over the hour (kW averaged, not summed)
    hourly = (
        cohort_5min.set_index("interval_start_utc")
        .resample("h")
        .mean(numeric_only=True)
        .reset_index()
    )

    buy_price = hourly["buy_price"].round(6).tolist()
    sell_price = hourly["sell_price"].round(6).tolist()
    return {
        "intervals": hourly["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
        "flex_up_kw": hourly["flex_up_kw"].round(2).tolist(),
        "flex_down_kw": hourly["flex_down_kw"].round(2).tolist(),
        "buy_price": buy_price,
        "sell_price": sell_price,
        "price_signal": buy_price,  # back-compat alias
        "price_unit": "$/kWh",
    }


def compute_price_response(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 2: Dual price-response curves, faceted by region.

    Splits the elasticity into two physically-distinct relationships:
      - import_curve: grid_import_kw vs buy_price_aud_per_kwh, where net_import_kw > 0
      - export_curve: grid_export_kw vs sell_price_aud_per_kwh, where net_import_kw < 0
        (grid_export_kw is reported as a positive magnitude)

    A single regression through both regimes mixes demand elasticity (negative slope)
    and supply elasticity (positive slope) and is meaningless. Splitting them lets each
    curve be estimated cleanly from open data.
    """
    empty_region = {
        "import": {"price": [], "power": []},
        "export": {"price": [], "power": []},
    }
    if df.empty:
        return {
            "regions": {r: empty_region for r in NEM_REGIONS},
            "price_unit": "$/kWh",
            "power_unit": "kW",
        }

    result: dict[str, Any] = {"regions": {}, "price_unit": "$/kWh", "power_unit": "kW"}
    for region in NEM_REGIONS:
        region_df = df[df["region"] == region]
        if region_df.empty:
            result["regions"][region] = empty_region
            continue

        importing = region_df[region_df["net_import_kw"] > 0]
        exporting = region_df[region_df["net_import_kw"] < 0]

        if not importing.empty:
            imp_sample = importing.sample(min(5000, len(importing)), random_state=42)
            import_block = {
                "price": imp_sample["price_signal_seen"].round(6).tolist(),
                "power": imp_sample["net_import_kw"].round(3).tolist(),
            }
        else:
            import_block = {"price": [], "power": []}

        if not exporting.empty:
            exp_sample = exporting.sample(min(5000, len(exporting)), random_state=42)
            export_block = {
                "price": exp_sample["price_export_seen"].round(6).tolist(),
                # report export as positive magnitude
                "power": (-exp_sample["net_import_kw"]).round(3).tolist(),
            }
        else:
            export_block = {"price": [], "power": []}

        result["regions"][region] = {
            "import": import_block,
            "export": export_block,
        }
    return result


def compute_curtailment_heatmap(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 3: Export curtailment heatmap (NEM region x hour-of-day).

    Quantifies the dollar cost of the static export envelope (typically 5 kW) by
    asking: at each interval, how much solar export was clipped by the envelope,
    and what was that energy worth at the prevailing sell price?

    For each 5-minute interval:
        export_kw      = max(-net_import_kw, 0)
        cap_kw         = envelope_export_limit_kw
        curtailed_kw   = max(export_kw - cap_kw, 0)         # always >= 0
        curtailed_kwh  = curtailed_kw * (interval_seconds / 3600)
        curtailed_aud  = curtailed_kwh * max(price_export_seen, 0)

    Note: where actual export already meets or exceeds the cap by other means
    (i.e. observed export exceeds cap), curtailed_kw is reported as 0 because
    the dispatch went through. The heatmap captures *unrealised* export only when
    the envelope was the binding constraint, which we approximate as intervals
    where exporting households were also at-or-near the cap. As a simple,
    monotone proxy we report the gap between the cap and the *flex_available_up*
    of solar that wanted to export. Without that field plumbed end-to-end we
    fall back to a conservative measure: for net-exporting intervals where
    export_kw equals the cap (within 0.05 kW), assume curtailment equals
    flex_available_down available to the EV/battery sink that did NOT absorb it,
    capped at solar headroom. To keep this aggregator simple and verifiable, we
    report two complementary quantities and let the dashboard show both:
      - at_cap_share:  share of intervals where export was within 0.05 kW of cap
      - curtailed_aud: estimated $ lost, conservative lower bound

    The conservative $ estimate uses:
        gap_kw = max( (solar_kw - house_load_kw) - cap_kw, 0 )
    which is the portion of net solar that exceeded the static cap regardless
    of battery/EV soak. This isolates the static-cap cost cleanly.
    """
    if df.empty:
        return {
            "regions": [],
            "hours": list(range(24)),
            "curtailed_kwh": [],
            "curtailed_aud": [],
            "at_cap_share": [],
            "price_unit": "$/kWh",
        }

    df = df.copy()
    df["hour"] = df["interval_start_utc"].dt.hour

    # Conservative kW gap: net solar above the static cap, regardless of soak.
    net_solar_kw = (df["solar_kw"] - df["house_load_kw"]).clip(lower=0.0)
    df["gap_kw"] = (net_solar_kw - df["envelope_export_limit_kw"]).clip(lower=0.0)
    df["curtailed_kwh"] = df["gap_kw"] * (INTERVAL_SECONDS / 3600.0)
    sell_price_pos = df["price_export_seen"].clip(lower=0.0)
    df["curtailed_aud"] = df["curtailed_kwh"] * sell_price_pos

    # at_cap indicator: actual export within 5% of envelope cap
    actual_export_kw = (-df["net_import_kw"]).clip(lower=0.0)
    cap = df["envelope_export_limit_kw"].replace(0, pd.NA)
    df["at_cap"] = (
        (actual_export_kw > 0)
        & ((cap - actual_export_kw).abs() <= 0.05 * cap)
    ).fillna(False).astype(float)

    grouped = (
        df.groupby(["region", "hour"])
        .agg(
            curtailed_kwh=("curtailed_kwh", "sum"),
            curtailed_aud=("curtailed_aud", "sum"),
            at_cap_share=("at_cap", "mean"),
        )
    )

    # Order rows by canonical NEM region order; only include regions present in data.
    present_regions = set(df["region"].unique().tolist())
    regions = [r for r in NEM_REGIONS if r in present_regions]

    def pivot_field(field: str, fill: float) -> list[list[float]]:
        pivot = grouped[field].unstack(fill_value=fill)
        for h in range(24):
            if h not in pivot.columns:
                pivot[h] = fill
        pivot = pivot.reindex(regions, fill_value=fill)
        pivot = pivot[sorted(pivot.columns)]
        return pivot.round(4).values.tolist()

    return {
        "regions": regions,
        "hours": list(range(24)),
        "curtailed_kwh": pivot_field("curtailed_kwh", 0.0),
        "curtailed_aud": pivot_field("curtailed_aud", 0.0),
        "at_cap_share": pivot_field("at_cap_share", 0.0),
        "total_curtailed_kwh": float(round(df["curtailed_kwh"].sum(), 3)),
        "total_curtailed_aud": float(round(df["curtailed_aud"].sum(), 2)),
        "price_unit": "$/kWh",
    }


def compute_counterfactual(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 4: Asymmetric counterfactual savings ledger (schema v2.0, $/kWh).

    Formula per 5-min interval:
        effective_price_kwh = price_signal_seen if net_import_kw > 0 else price_export_seen
        saving_aud = (naive_baseline_kw - net_import_kw) * effective_price_kwh * (300 / 3600)

    No /1000 scaling (prices are already in $/kWh).
    The (300/3600) factor converts kW over 5 minutes to kWh.
    """
    if df.empty:
        return {
            "intervals": [],
            "interval_savings": [],
            "cumulative_savings": [],
            "total_savings": 0.0,
            "price_unit": "$/kWh",
        }

    df = df.copy().sort_values("interval_start_utc")

    price_used = df["price_signal_seen"].where(
        df["net_import_kw"] > 0, df["price_export_seen"]
    )

    df["savings"] = (
        (df["naive_baseline_kw"] - df["net_import_kw"])
        * price_used
        * (INTERVAL_SECONDS / 3600.0)
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
        "price_unit": "$/kWh",
    }


def compute_buy_sell_spread(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 5: Buy/sell price spread by region ($/kWh)."""
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
            },
            "price_unit": "$/kWh",
        }

    result: dict[str, Any] = {"regions": {}, "price_unit": "$/kWh"}

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

        neg_fit_mask = hourly["price_export_seen"] < 0
        neg_fit_intervals = (
            hourly.loc[neg_fit_mask, "interval_start_utc"]
            .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            .tolist()
        )

        result["regions"][region] = {
            "intervals": hourly["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(),
            "buy_price": hourly["price_signal_seen"].round(6).tolist(),
            "sell_price": hourly["price_export_seen"].round(6).tolist(),
            "negative_fit_intervals": neg_fit_intervals,
        }

    return result


# ---------------------------------------------------------------------------
# New dashboard views (tabs 6-7)
# ---------------------------------------------------------------------------

def compute_assets_summary(
    df: pd.DataFrame, assets_df: pd.DataFrame
) -> dict[str, Any]:
    """Tab 6: Assets and V2G.

    Three views:
    a) Asset mix donut: cohort total kWh by kind (stationary_battery vs ev)
    b) V2G plug duty cycle: time series of % cohort EVs in each connection state
    c) V2G dispatch share: stacked area of total cohort setpoint_kw by kind

    Returns site/data/assets_summary.json.
    """
    if assets_df.empty:
        return {
            "asset_mix": {"stationary_battery_kwh": 0.0, "ev_kwh": 0.0},
            "v2g_duty_cycle": {"intervals": [], "unplugged_pct": [], "plugged_idle_pct": [], "charging_pct": [], "discharging_pct": [], "driving_pct": []},
            "v2g_dispatch_share": {"intervals": [], "battery_kw": [], "ev_kw": []},
        }

    # a) Asset mix donut
    asset_mix: dict[str, float] = {"stationary_battery_kwh": 0.0, "ev_kwh": 0.0}
    # Take most recent SOC snapshot per asset per household and compute stored kWh
    latest_assets = (
        assets_df.sort_values("interval_start_utc")
        .groupby(["household_id", "asset_id"])
        .last()
        .reset_index()
    )
    for _, row in latest_assets.iterrows():
        kwh = row["capacity_kwh"] * row["soc_pct"] / 100.0
        if row["kind"] == "stationary_battery":
            asset_mix["stationary_battery_kwh"] += kwh
        elif row["kind"] == "ev":
            asset_mix["ev_kwh"] += kwh
    asset_mix["stationary_battery_kwh"] = round(asset_mix["stationary_battery_kwh"], 2)
    asset_mix["ev_kwh"] = round(asset_mix["ev_kwh"], 2)

    # b) V2G plug duty cycle (EV assets only)
    ev_df = assets_df[assets_df["kind"] == "ev"].copy()
    duty_cycle: dict[str, Any] = {
        "intervals": [],
        "unplugged_pct": [],
        "plugged_idle_pct": [],
        "charging_pct": [],
        "discharging_pct": [],
        "driving_pct": [],
    }

    if not ev_df.empty and "connection_state" in ev_df.columns:
        ev_df["hour"] = ev_df["interval_start_utc"].dt.floor("h")
        states = ["unplugged", "plugged_idle", "charging", "discharging", "driving"]
        state_cols = []
        for s in states:
            ev_df[f"is_{s}"] = (ev_df["connection_state"] == s).astype(float)
            state_cols.append(f"is_{s}")

        hourly_ev = (
            ev_df.groupby("hour")[state_cols]
            .mean()
            .reset_index()
        )

        duty_cycle["intervals"] = hourly_ev["hour"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
        for s in states:
            duty_cycle[f"{s}_pct"] = (hourly_ev[f"is_{s}"] * 100).round(1).tolist()

    # c) V2G dispatch share: hourly mean cohort setpoint by kind (signed kW).
    # Two-stage aggregation:
    #   1. cohort sum at each 5-min interval (sum across households+assets of
    #      that kind), so simultaneous dispatch by multiple assets stacks.
    #   2. time-mean across the 12 sub-intervals of each hour, so the result
    #      is reported in kW (not 12*kW). Mirrors the v0.5.1 fix applied to
    #      compute_flex_stack: a naive .sum() across both axes would 12x-
    #      inflate the value (a 25 kW DCEV would appear as ~300 kW).
    dispatch_share: dict[str, Any] = {"intervals": [], "battery_kw": [], "ev_kw": []}
    if not assets_df.empty and "setpoint_kw" in assets_df.columns:
        assets_df2 = assets_df.copy()
        assets_df2["setpoint_kw"] = pd.to_numeric(assets_df2["setpoint_kw"], errors="coerce").fillna(0.0)
        assets_df2["hour"] = assets_df2["interval_start_utc"].dt.floor("h")

        # Step 1: cohort sum per 5-min interval, per kind.
        cohort_5min = (
            assets_df2.groupby(["interval_start_utc", "kind"])["setpoint_kw"]
            .sum()
            .reset_index()
        )
        cohort_5min["hour"] = cohort_5min["interval_start_utc"].dt.floor("h")

        # Step 2: time-mean over the hour (kW averaged, not summed).
        hourly_dispatch = (
            cohort_5min.groupby(["hour", "kind"])["setpoint_kw"]
            .mean()
            .unstack(fill_value=0.0)
            .reset_index()
        )

        dispatch_share["intervals"] = hourly_dispatch["hour"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
        dispatch_share["battery_kw"] = (
            hourly_dispatch.get("stationary_battery", pd.Series(dtype=float)).round(2).tolist()
        )
        dispatch_share["ev_kw"] = (
            hourly_dispatch.get("ev", pd.Series(dtype=float)).round(2).tolist()
        )

    return {
        "asset_mix": asset_mix,
        "v2g_duty_cycle": duty_cycle,
        "v2g_dispatch_share": dispatch_share,
    }


def compute_shadow_prices(df: pd.DataFrame) -> dict[str, Any]:
    """Tab 7: Shadow prices.

    Two views (all values in $/kWh):
    a) Shadow price distribution: mean shadow_energy_price by hour-of-day (violin proxy)
    b) Envelope shadow heatmap: NEM region x hour grid of binding envelope shadow prices

    The envelope heatmaps are populated from HAEO's load and solar forecast-limit
    shadow prices, which bind almost continuously and reflect where the forecast
    envelope is actually constraining net import (load side) or net export (solar
    side). The grid_max_import/export_power shadows are also published as separate
    fields for completeness but are rarely non-zero on Mark's 30 kW envelope.

    Returns site/data/shadow_prices.json.
    """
    empty = {
        "shadow_by_hour": {
            "hours": list(range(24)),
            "mean_shadow_energy_price": [0.0] * 24,
            "p25_shadow_energy_price": [0.0] * 24,
            "p75_shadow_energy_price": [0.0] * 24,
        },
        "envelope_shadow_heatmap": {
            "regions": [],
            "hours": list(range(24)),
            "import_shadow": [],
            "export_shadow": [],
            "import_source": "shadow_load_forecast_price",
            "export_source": "shadow_solar_forecast_price",
        },
        "grid_envelope_shadow_heatmap": {
            "regions": [],
            "hours": list(range(24)),
            "import_shadow": [],
            "export_shadow": [],
            "import_source": "shadow_envelope_import_price",
            "export_source": "shadow_envelope_export_price",
        },
        "price_unit": "$/kWh",
        "explainer": (
            "Shadow energy price is the LP dual on the switchboard power-balance "
            "constraint: the marginal cost of one extra kWh of net energy at the "
            "meter for the current dispatch interval. Envelope shadow prices show "
            "where HAEO's forecast envelope is actually constraining flex, in "
            "dollar terms. Import side draws on the load forecast limit; export "
            "side draws on the solar forecast limit."
        ),
    }

    if df.empty:
        return empty

    df = df.copy()
    df["hour"] = df["interval_start_utc"].dt.hour

    # a) Shadow energy price distribution by hour (all $/kWh)
    shadow_by_hour: dict[str, Any] = {"hours": list(range(24))}
    shadow_col = "shadow_energy_price"
    if shadow_col in df.columns and df[shadow_col].notna().any():
        stats = (
            df.groupby("hour")[shadow_col]
            .agg(mean="mean", p25=lambda x: x.quantile(0.25), p75=lambda x: x.quantile(0.75))
            .reindex(range(24), fill_value=0.0)
        )
        shadow_by_hour["mean_shadow_energy_price"] = stats["mean"].round(6).tolist()
        shadow_by_hour["p25_shadow_energy_price"] = stats["p25"].round(6).tolist()
        shadow_by_hour["p75_shadow_energy_price"] = stats["p75"].round(6).tolist()
    else:
        shadow_by_hour["mean_shadow_energy_price"] = [0.0] * 24
        shadow_by_hour["p25_shadow_energy_price"] = [0.0] * 24
        shadow_by_hour["p75_shadow_energy_price"] = [0.0] * 24

    def _pivot_one(col: str) -> tuple[list[str], list[list[float]]]:
        """Pivot a single shadow-price column into (regions, 24h grid).

        Returns ([], []) if the column is missing so the caller can decide
        whether to fall back to a zero-filled side.
        """
        if col not in df.columns:
            return [], []
        series = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        pivot = (
            series.groupby([df["region"], df["hour"]])
            .mean()
            .unstack(fill_value=0.0)
        )
        for h in range(24):
            if h not in pivot.columns:
                pivot[h] = 0.0
        pivot = pivot[sorted(pivot.columns)]
        return pivot.index.tolist(), pivot.round(6).values.tolist()

    def _pivot_pair(import_col: str, export_col: str) -> dict[str, Any]:
        """Build a NEM-region x hour heatmap pair from two shadow-price columns.

        All inputs and outputs are $/kWh; no unit conversion is applied.
        Each side is built independently: if only one column is present we still
        publish the available side and warn-log the missing one rather than silently
        dropping both.
        """
        result: dict[str, Any] = {
            "regions": [],
            "hours": list(range(24)),
            "import_shadow": [],
            "export_shadow": [],
            "import_source": import_col,
            "export_source": export_col,
        }

        import_missing = import_col not in df.columns
        export_missing = export_col not in df.columns
        if import_missing and export_missing:
            return result
        if import_missing:
            _LOG.warning(
                "Shadow heatmap: import column %r missing, publishing export side only",
                import_col,
            )
        if export_missing:
            _LOG.warning(
                "Shadow heatmap: export column %r missing, publishing import side only",
                export_col,
            )

        import_regions, import_grid = _pivot_one(import_col)
        export_regions, export_grid = _pivot_one(export_col)

        # Union across both sides, ordered by canonical NEM region order.
        seen = set(import_regions) | set(export_regions)
        all_regions = [r for r in NEM_REGIONS if r in seen]
        zero_row = [0.0] * 24

        def _row_for(r: str, regions: list[str], grid: list[list[float]]) -> list[float]:
            if r in regions:
                return grid[regions.index(r)]
            return zero_row

        result["regions"] = all_regions
        result["import_shadow"] = [_row_for(r, import_regions, import_grid) for r in all_regions]
        result["export_shadow"] = [_row_for(r, export_regions, export_grid) for r in all_regions]
        return result

    # b) Primary envelope heatmap: HAEO forecast limit shadows (almost always binding)
    forecast_heatmap = _pivot_pair(
        "shadow_load_forecast_price",
        "shadow_solar_forecast_price",
    )

    # c) Secondary heatmap: grid envelope shadows (rare on a 30 kW envelope)
    grid_envelope_heatmap = _pivot_pair(
        "shadow_envelope_import_price",
        "shadow_envelope_export_price",
    )

    return {
        "shadow_by_hour": shadow_by_hour,
        "envelope_shadow_heatmap": forecast_heatmap,
        "grid_envelope_shadow_heatmap": grid_envelope_heatmap,
        "price_unit": "$/kWh",
        "explainer": (
            "Shadow energy price is the LP dual on the switchboard power-balance "
            "constraint: the marginal cost of one extra kWh of net energy at the "
            "meter for the current dispatch interval. Envelope shadow prices show "
            "where HAEO's forecast envelope is actually constraining flex, in "
            "dollar terms. Import side draws on the load forecast limit; export "
            "side draws on the solar forecast limit."
        ),
    }


# ---------------------------------------------------------------------------
# Status JSON
# ---------------------------------------------------------------------------

def compute_status(df: pd.DataFrame) -> dict[str, Any]:
    """Compute status metrics for the dashboard header and shields.io badges."""
    cohort_size = df["household_id"].nunique() if not df.empty else 0
    total_intervals = len(df)
    total_savings = 0.0

    if not df.empty:
        price_used = df["price_signal_seen"].where(
            df["net_import_kw"] > 0, df["price_export_seen"]
        )
        total_savings = float((
            (df["naive_baseline_kw"] - df["net_import_kw"])
            * price_used
            * (INTERVAL_SECONDS / 3600.0)
        ).sum().round(2))

    return {
        "cohort_size": cohort_size,
        "total_intervals": total_intervals,
        "total_savings_aud": total_savings,
        "last_updated": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regions": sorted(df["region"].unique().tolist()) if not df.empty else [],
        "schema_version": "2.0",
    }


# ---------------------------------------------------------------------------
# JSON sanitiser
# ---------------------------------------------------------------------------

def _json_safe(value: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so output is RFC-8259 JSON.

    Browsers reject the ``NaN`` literal in ``JSON.parse`` even though Python's
    ``json`` module emits it by default. Any feed containing one bad value
    causes the dashboard to fall back to sample data and show the demo banner.
    Pandas/numpy NA values also get normalised to None here.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    # pandas / numpy scalars
    try:
        if pd.isna(value):  # type: ignore[arg-type]
            return None
    except (TypeError, ValueError):
        pass
    return value


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full aggregation pipeline."""
    _LOG.info("Starting NEM Flex Telemetry aggregation (schema v2.0).")
    _LOG.info("Repo root: %s", REPO_ROOT)

    df = load_all_jsonl()
    _LOG.info("Total records after deduplication: %d", len(df))

    for resolution in ("5min", "hourly", "daily"):
        write_parquet_by_date(df, resolution)

    # Expand assets for tab 6
    assets_df = expand_assets(df)
    _LOG.info("Expanded %d asset records.", len(assets_df))

    SITE_DATA.mkdir(parents=True, exist_ok=True)

    views = {
        "cohort_flex_stack.json": compute_flex_stack(df),
        "price_response.json": compute_price_response(df),
        "curtailment_heatmap.json": compute_curtailment_heatmap(df),
        "counterfactual.json": compute_counterfactual(df),
        "buy_sell_spread.json": compute_buy_sell_spread(df),
        "assets_summary.json": compute_assets_summary(df, assets_df),
        "shadow_prices.json": compute_shadow_prices(df),
        "status.json": compute_status(df),
    }

    for filename, data in views.items():
        out_path = SITE_DATA / filename
        # Sanitise NaN/Inf to null before writing; browsers reject `NaN` literals
        # in JSON.parse() and the dashboard fell back to sample data when AEMO
        # spot intervals were missing.
        clean = _json_safe(data)
        with out_path.open("w") as f:
            json.dump(clean, f, indent=2, allow_nan=False)
        _LOG.info("Wrote %s", out_path)

    _LOG.info("Aggregation complete.")


if __name__ == "__main__":
    main()
