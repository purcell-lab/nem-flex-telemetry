"""k-anonymity guardrails in the aggregator (#22).

Every published per-region series must be built from at least K_MIN_REGION
households, carry a `households` count, and published parquet must not carry
household_id.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pq = pytest.importorskip("pyarrow.parquet")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aggregate  # noqa: E402

# region -> number of households in the synthetic cohort
COHORT = {"NSW1": 3, "VIC1": 2, "QLD1": 1, "TAS1": 1}


def _cohort_df(cohort: dict[str, int] = COHORT, prefix: str = "200") -> "pd.DataFrame":
    """Two hours of 5-minute records per household, importing and exporting."""
    ts = pd.date_range("2026-10-08T00:00:00Z", periods=24, freq="5min")
    frames = []
    for region, n in cohort.items():
        for i in range(n):
            net = [1.5 if j % 2 else -6.0 for j in range(len(ts))]
            frames.append(pd.DataFrame({
                "interval_start_utc": ts,
                "household_id": f"{region}-{i}",
                "region": region,
                "postcode_prefix": prefix,
                "schema_version": "2.0",
                "net_import_kw": net,
                "solar_kw": 9.0,
                "house_load_kw": 1.0,
                "deferrable_load_kw": 0.0,
                "naive_baseline_kw": 1.0,
                "naive_baseline_method": "subtraction",
                "price_signal_seen": 0.25,
                "price_export_seen": 0.05,
                "envelope_import_limit_kw": 30.0,
                "envelope_export_limit_kw": 5.0,
                "flex_available_up_kw": 2.0,
                "flex_available_down_kw": 3.0,
                "shadow_energy_price": 0.08,
                "shadow_load_forecast_price": 0.01,
                "shadow_solar_forecast_price": -0.02,
                "shadow_envelope_import_price": 0.0,
                "shadow_envelope_export_price": 0.0,
            }))
    return pd.concat(frames, ignore_index=True)


def _published_region_series(df: "pd.DataFrame") -> list[tuple[str, dict, set[str]]]:
    """(view name, payload carrying privacy fields, regions with published data)."""
    out = []

    pr = aggregate.compute_price_response(df)
    out.append(("price_response", pr, {
        r for r, b in pr["regions"].items()
        if b["import"]["price"] or b["export"]["price"]
    }))

    bs = aggregate.compute_buy_sell_spread(df)
    out.append(("buy_sell_spread", bs, {
        r for r, b in bs["regions"].items() if b["intervals"]
    }))

    ch = aggregate.compute_curtailment_heatmap(df)
    out.append(("curtailment_heatmap", ch, set(ch["regions"])))

    sp = aggregate.compute_shadow_prices(df)
    for key in ("envelope_shadow_heatmap", "grid_envelope_shadow_heatmap"):
        out.append((key, sp[key], set(sp[key]["regions"])))
    return out


def _true_households(df: "pd.DataFrame", region: str, privacy: dict) -> int:
    """Count households behind a published series straight from the input."""
    if region == aggregate.NEM_ROLLUP_REGION:
        mask = df["region"].isin(privacy["suppressed_regions"])
    else:
        mask = df["region"] == region
    return df.loc[mask, "household_id"].nunique()


@pytest.mark.parametrize("k", [1, 2, 3, 5, 8])
def test_no_published_region_series_below_k(monkeypatch, k: int) -> None:
    """Fails if any published regional series has fewer than K_MIN_REGION households."""
    monkeypatch.setattr(aggregate, "K_MIN_REGION", k)
    df = _cohort_df()
    for name, payload, published in _published_region_series(df):
        assert payload["k_min_region"] == k, name
        assert set(payload["households"]) == published, name
        for region in published:
            n = _true_households(df, region, payload)
            assert n >= k, f"{name}: {region} published with {n} < k={k} households"
            assert payload["households"][region] == n, name
        for region in payload["suppressed_regions"]:
            assert region not in published, f"{name}: suppressed {region} still published"


def test_default_k_publishes_every_region_with_counts(monkeypatch) -> None:
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 1)
    for name, payload, published in _published_region_series(_cohort_df()):
        assert payload["households"] == COHORT, name
        assert payload["suppressed_regions"] == [], name
        assert payload["rolled_up_into"] is None, name
        assert payload["k_advisory"] == aggregate.K_ADVISORY, name
        assert published == set(COHORT), name


def test_k2_pools_single_household_regions_into_nem(monkeypatch) -> None:
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 2)
    pr = aggregate.compute_price_response(_cohort_df())
    assert pr["suppressed_regions"] == ["QLD1", "TAS1"]
    assert pr["rolled_up_into"] == "NEM"
    assert pr["households"] == {"NSW1": 3, "VIC1": 2, "NEM": 2}
    assert pr["regions"]["QLD1"]["suppressed"] is True
    assert pr["regions"]["QLD1"]["import"]["price"] == []
    assert pr["regions"]["NEM"]["import"]["price"]


def test_k5_rolls_everything_into_nem_and_k8_suppresses_all(monkeypatch) -> None:
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 5)
    ch = aggregate.compute_curtailment_heatmap(_cohort_df())
    assert ch["regions"] == ["NEM"]
    assert ch["households"] == {"NEM": 7}

    monkeypatch.setattr(aggregate, "K_MIN_REGION", 8)
    ch = aggregate.compute_curtailment_heatmap(_cohort_df())
    assert ch["regions"] == []
    assert ch["curtailed_kwh"] == []
    assert ch["total_curtailed_kwh"] == 0.0
    assert ch["suppressed_regions"] == ["NSW1", "QLD1", "VIC1", "TAS1"]
    assert ch["rolled_up_into"] is None
    assert ch["suppression_note"] == aggregate.SUPPRESSION_NOTE
    bs = aggregate.compute_buy_sell_spread(_cohort_df())
    assert all(b.get("suppressed") for r, b in bs["regions"].items() if r in COHORT)


def test_status_reports_thresholds(monkeypatch) -> None:
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 2)
    status = aggregate.compute_status(_cohort_df())
    assert status["cohort_size"] == 7
    k = status["k_anonymity"]
    assert k["k_min_region"] == 2
    assert k["k_min_prefix"] == aggregate.K_MIN_PREFIX
    assert k["households_by_region"] == {"NSW1": 3, "VIC1": 2, "NEM": 2}


def test_published_parquet_has_no_household_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(aggregate, "DATA_COHORT", tmp_path)
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 1)
    monkeypatch.setattr(aggregate, "K_MIN_PREFIX", 5)
    # Six households share prefix 200; one sits alone in prefix 400.
    df = pd.concat([
        _cohort_df({"NSW1": 6}, prefix="200"),
        _cohort_df({"QLD1": 1}, prefix="400"),
    ], ignore_index=True)
    for resolution in ("5min", "hourly", "daily"):
        aggregate.write_parquet_by_date(df, resolution)
        files = list((tmp_path / resolution).rglob("*.parquet"))
        assert files, resolution
        for path in files:
            table = pq.read_table(path)
            assert "household_id" not in table.column_names, path
            by_region: dict[str, set] = {}
            for region, prefix in zip(
                table.column("region").to_pylist(),
                table.column("postcode_prefix").to_pylist(),
            ):
                by_region.setdefault(region, set()).add(prefix)
            assert by_region["NSW1"] == {"200"}, (resolution, path)
            assert by_region["QLD1"] == {None}, (resolution, path)


def test_parquet_honours_region_threshold(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(aggregate, "DATA_COHORT", tmp_path)
    monkeypatch.setattr(aggregate, "K_MIN_REGION", 2)
    aggregate.write_parquet_by_date(_cohort_df(), "5min")
    regions = set()
    for path in tmp_path.rglob("*.parquet"):
        regions |= set(pq.read_table(path).column("region").to_pylist())
    assert regions == {"NSW1", "VIC1", "NEM"}


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("NEM_FLEX_K_MIN_REGION", "5")
    assert aggregate._env_int("NEM_FLEX_K_MIN_REGION", 1) == 5
    monkeypatch.setenv("NEM_FLEX_K_MIN_REGION", "zero")
    assert aggregate._env_int("NEM_FLEX_K_MIN_REGION", 1) == 1
    monkeypatch.setenv("NEM_FLEX_K_MIN_REGION", "0")
    assert aggregate._env_int("NEM_FLEX_K_MIN_REGION", 1) == 1
    monkeypatch.delenv("NEM_FLEX_K_MIN_REGION")
    assert aggregate._env_int("NEM_FLEX_K_MIN_REGION", 1) == 1


def test_parquet_rewrite_clears_stale_files(monkeypatch, tmp_path) -> None:
    """A date no longer published must not leave an old file behind."""
    monkeypatch.setattr(aggregate, "DATA_COHORT", tmp_path)
    stale = tmp_path / "daily" / "2020" / "01" / "01.parquet"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    aggregate.write_parquet_by_date(_cohort_df(), "daily")
    assert not stale.exists()
    assert list((tmp_path / "daily").rglob("*.parquet"))
