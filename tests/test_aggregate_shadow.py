"""Shadow-price dashboard aggregation uses medians (#30)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aggregate  # noqa: E402


def _df(values: list[float]) -> "pd.DataFrame":
    ts = pd.date_range("2026-10-08T04:00:00Z", periods=len(values), freq="5min")
    return pd.DataFrame(
        {
            "interval_start_utc": ts,
            "region": "NSW1",
            "shadow_energy_price": values,
            "shadow_load_forecast_price": values,
            "shadow_solar_forecast_price": [abs(v) for v in values],
            "shadow_envelope_import_price": 0.0,
            "shadow_envelope_export_price": values,
        }
    )


def test_one_bound_episode_does_not_move_the_median() -> None:
    """Three -9.77 duals among nine normal ones leave the hourly median alone."""
    values = [0.08] * 9 + [-5.27, -9.77, -5.27]
    out = aggregate.compute_shadow_prices(_df(values))
    hour = out["shadow_by_hour"]
    assert hour["median_shadow_energy_price"][4] == pytest.approx(0.08)
    assert hour["mean_shadow_energy_price"][4] < -1.0
    assert hour["bound_intervals"][4] == 3
    assert hour["bound_threshold"] == aggregate.SHADOW_BOUND_THRESHOLD


def test_empty_frame_has_median_keys() -> None:
    out = aggregate.compute_shadow_prices(pd.DataFrame())
    assert out["shadow_by_hour"]["median_shadow_energy_price"] == [0.0] * 24
    assert out["shadow_by_hour"]["bound_intervals"] == [0] * 24
