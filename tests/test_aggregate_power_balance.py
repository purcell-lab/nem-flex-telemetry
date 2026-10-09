"""Power-balance data-quality check in the aggregator (#20)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aggregate  # noqa: E402

BALANCED_ID = "11111111-aaaa-bbbb-cccc-000000000001"
BROKEN_ID = "22222222-aaaa-bbbb-cccc-000000000002"


def _record(
    household_id: str,
    region: str,
    ts: pd.Timestamp,
    net_import_kw: float,
    solar_kw: float,
    house_load_kw: float,
    setpoints: list[float | None],
    deferrable_load_kw: float = 0.0,
) -> dict:
    return {
        "household_id": household_id,
        "region": region,
        "interval_start_utc": ts,
        "net_import_kw": net_import_kw,
        "solar_kw": solar_kw,
        "house_load_kw": house_load_kw,
        "deferrable_load_kw": deferrable_load_kw,
        "naive_baseline_kw": 0.0,
        "price_signal_seen": 0.1,
        "price_export_seen": 0.05,
        "assets": [
            {"asset_id": f"a{i}", "kind": "stationary_battery", "setpoint_kw": sp}
            for i, sp in enumerate(setpoints)
        ],
    }


def _frame(n: int = 12) -> "pd.DataFrame":
    """One balanced QLD1 household and one NSW1 household with house load 0."""
    ts = pd.date_range("2026-10-08T00:00:00Z", periods=n, freq="5min")
    rows = []
    for t in ts:
        # 1.0 import + 4.0 solar = 2.0 house + 0.5 deferrable + 3.0 battery charging - 0.5 EV discharging
        rows.append(_record(BALANCED_ID, "QLD1", t, 1.0, 4.0, 2.0, [3.0, -0.5], 0.5))
        # Same physical flows but house load mapped to 0 kW.
        rows.append(_record(BROKEN_ID, "NSW1", t, 1.0, 4.0, 0.0, [3.0, -0.5], 0.5))
    return pd.DataFrame(rows)


def test_residual_subtracts_charging_setpoint() -> None:
    ts = pd.Timestamp("2026-10-08T00:00:00Z")
    df = pd.DataFrame([
        # Charging at 3 kW from solar: balanced.
        _record(BALANCED_ID, "QLD1", ts, 0.0, 4.0, 1.0, [3.0]),
        # Discharging 2 kW to cover load and export: balanced.
        _record(BALANCED_ID, "QLD1", ts, -0.5, 0.0, 1.5, [-2.0]),
        # Null setpoint counts as 0 kW.
        _record(BALANCED_ID, "QLD1", ts, 1.0, 0.0, 1.0, [None]),
        # No assets at all.
        _record(BALANCED_ID, "QLD1", ts, 0.2, 1.0, 1.2, []),
    ])
    residual = aggregate.power_balance_residual(df)
    assert residual.tolist() == pytest.approx([0.0, 0.0, 0.0, 0.0])


def test_residual_sign_flags_wrong_asset_sign() -> None:
    # Adding the setpoint (the formula quoted in #20) would close this record;
    # the schema's positive = charging convention leaves a 6 kW residual.
    ts = pd.Timestamp("2026-10-08T00:00:00Z")
    df = pd.DataFrame([_record(BALANCED_ID, "QLD1", ts, 0.0, 4.0, 7.0, [-3.0])])
    assert aggregate.power_balance_residual(df).iat[0] == pytest.approx(0.0)
    df = pd.DataFrame([_record(BALANCED_ID, "QLD1", ts, 0.0, 4.0, 1.0, [-3.0])])
    assert aggregate.power_balance_residual(df).iat[0] == pytest.approx(6.0)


def test_inverted_setpoint_diagnostic() -> None:
    # Publisher reports charging as negative: flagged under the schema
    # convention, but the inverted-sign diagnostic closes the balance.
    ts = pd.date_range("2026-10-08T00:00:00Z", periods=4, freq="5min")
    df = pd.DataFrame([_record(BALANCED_ID, "QLD1", t, 0.0, 4.0, 1.0, [-3.0]) for t in ts])
    hh = aggregate.household_power_balance(df)
    assert hh.loc[BALANCED_ID, "flagged"]
    assert hh.loc[BALANCED_ID, "mean_abs_residual_kw"] == pytest.approx(6.0)
    assert hh.loc[BALANCED_ID, "mean_abs_residual_if_setpoint_inverted_kw"] == pytest.approx(0.0)


def test_balanced_household_not_flagged_and_zero_load_flagged() -> None:
    hh = aggregate.household_power_balance(_frame())
    assert not hh.loc[BALANCED_ID, "flagged"]
    assert hh.loc[BALANCED_ID, "mean_abs_residual_kw"] == pytest.approx(0.0)
    assert hh.loc[BALANCED_ID, "zero_house_load_intervals"] == 0

    assert hh.loc[BROKEN_ID, "flagged"]
    assert hh.loc[BROKEN_ID, "mean_residual_kw"] == pytest.approx(2.0)
    assert hh.loc[BROKEN_ID, "p90_abs_residual_kw"] == pytest.approx(2.0)
    assert hh.loc[BROKEN_ID, "share_over_threshold"] == pytest.approx(1.0)
    assert hh.loc[BROKEN_ID, "zero_house_load_intervals"] == 12


def test_threshold_is_respected(monkeypatch) -> None:
    monkeypatch.setattr(aggregate, "POWER_BALANCE_RESIDUAL_KW", 2.5)
    hh = aggregate.household_power_balance(_frame())
    assert not hh["flagged"].any()


def test_public_output_has_no_household_ids(caplog) -> None:
    df = _frame()
    with caplog.at_level("WARNING", logger="aggregate"):
        out = aggregate.compute_power_balance(df)

    assert out["cohort"]["households"] == 2
    assert out["cohort"]["flagged"] == 1
    assert out["regions"]["NSW1"]["flagged"] == 1
    assert out["regions"]["QLD1"]["flagged"] == 0
    assert out["regions"]["NSW1"]["zero_house_load_intervals"] == 12

    published = json.dumps(aggregate._json_safe(out))
    assert BALANCED_ID not in published and BROKEN_ID not in published
    assert "household_id" not in published

    # Warning names the region and a short hash, never the raw ID.
    assert "NSW1" in caplog.text
    assert aggregate._household_tag(BROKEN_ID) in caplog.text
    assert BROKEN_ID not in caplog.text


def test_status_counts_flagged_households() -> None:
    status = aggregate.compute_status(_frame())
    assert status["power_balance_flagged"] == 1


def test_empty_frame() -> None:
    df = pd.DataFrame(columns=aggregate.REQUIRED_FIELDS + ["household_id"])
    out = aggregate.compute_power_balance(df)
    assert out["cohort"]["households"] == 0
    assert out["regions"] == {}
    assert aggregate.compute_status(df)["power_balance_flagged"] == 0
