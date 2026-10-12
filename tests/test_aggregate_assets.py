"""Placeholder assets from the pre-#15 setup form are ignored (#15)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aggregate  # noqa: E402


def _asset(asset_id: str, kind: str, capacity: float, soc: float = 50.0) -> dict:
    return {
        "asset_id": asset_id,
        "kind": kind,
        "bidirectional_capable": True,
        "capacity_kwh": capacity,
        "soc_pct": soc,
        "setpoint_kw": 0.0,
        "available_up_kw": 0.0,
        "available_down_kw": 0.0,
        "shadow_power_balance_price": None,
        **({"connection_state": "unplugged"} if kind == "ev" else {}),
    }


def _df(assets: list[dict]) -> "pd.DataFrame":
    return pd.DataFrame(
        [
            {
                "interval_start_utc": pd.Timestamp("2026-10-08T04:00:00Z"),
                "household_id": "h1",
                "region": "QLD1",
                "postcode_prefix": "456",
                "assets": assets,
            }
        ]
    )


def test_placeholder_assets_are_dropped() -> None:
    """A one-EV household that typed 0.1 kWh for EV2 publishes one EV only."""
    df = _df(
        [
            _asset("home_battery", "stationary_battery", 13.5),
            _asset("ev1", "ev", 60.0),
            _asset("ev2", "ev", 0.1, soc=0.0),
        ]
    )
    assets_df = aggregate.expand_assets(df)
    assert sorted(assets_df["asset_id"]) == ["ev1", "home_battery"]

    summary = aggregate.compute_assets_summary(df, assets_df)
    assert summary["asset_mix"]["ev_kwh"] == pytest.approx(30.0)
    assert summary["asset_mix"]["stationary_battery_kwh"] == pytest.approx(6.75)
    # Duty cycle is over real EVs only: ev1 unplugged = 100 %.
    assert summary["v2g_duty_cycle"]["unplugged_pct"] == [100.0]


def test_only_placeholders_gives_empty_frame() -> None:
    df = _df([_asset("home_battery", "stationary_battery", 0.1)])
    assert aggregate.expand_assets(df).empty
