"""Regenerate site/data/sample.json with v0.5.0 shapes:
  - price_response: dual import/export curves per region
  - curtailment_heatmap: replaces envelope_heatmap

Other tabs are preserved from existing sample.
"""
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "site" / "data" / "sample.json"

random.seed(7)

with SAMPLE.open() as f:
    d = json.load(f)

REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]


def synth_curve(n, slope, intercept, x_lo, x_hi, noise):
    pts = []
    for _ in range(n):
        x = random.uniform(x_lo, x_hi)
        y = intercept + slope * x + random.gauss(0, noise)
        pts.append((round(x, 6), round(y, 3)))
    return pts


regions_block = {}
for r in REGIONS:
    # Import curve: negative elasticity. As buy price rises, import falls.
    # Buy price 0.05 to 0.55 $/kWh; import 0 to ~12 kW
    imp_pts = synth_curve(180, slope=-15.0, intercept=10.0,
                          x_lo=0.05, x_hi=0.55, noise=1.4)
    imp_pts = [(x, max(y, 0.0)) for (x, y) in imp_pts]

    # Export curve: positive elasticity. As sell price rises, export rises.
    # Sell price -0.05 to 0.35 $/kWh; export 0 to ~12 kW (positive magnitude)
    exp_pts = synth_curve(160, slope=30.0, intercept=2.0,
                          x_lo=-0.05, x_hi=0.35, noise=1.6)
    exp_pts = [(x, max(y, 0.0)) for (x, y) in exp_pts]

    regions_block[r] = {
        "import": {"price": [p[0] for p in imp_pts], "power": [p[1] for p in imp_pts]},
        "export": {"price": [p[0] for p in exp_pts], "power": [p[1] for p in exp_pts]},
    }

d["price_response"] = {
    "regions": regions_block,
    "price_unit": "$/kWh",
    "power_unit": "kW",
}

# Curtailment heatmap: 3 postcodes x 24 hours, daylight hours (UTC 22..09 ish for QLD)
postcodes = ["455", "456", "457"]
hours = list(range(24))


def daylight_curve(hour):
    # gaussian peak at hour 03 UTC (13 Brisbane), width 4h
    return math.exp(-((hour - 3) ** 2) / (2 * 4 * 4))


curt_kwh = []
curt_aud = []
at_cap = []
for pc in postcodes:
    row_kwh, row_aud, row_cap = [], [], []
    for h in hours:
        d_factor = daylight_curve(h)
        # ~ peak 1.2 kWh per cohort-interval-hour, falling off at night
        kwh = round(d_factor * 1.2 * (1 + random.uniform(-0.1, 0.15)), 4)
        # sell price varies by hour, lowest at midday
        sell = max(0.02, 0.18 - 0.16 * d_factor + random.uniform(-0.02, 0.02))
        aud = round(kwh * sell, 4)
        cap_share = round(min(0.95, d_factor * 0.85 + random.uniform(0, 0.1)), 3) if d_factor > 0.05 else 0.0
        row_kwh.append(kwh)
        row_aud.append(aud)
        row_cap.append(cap_share)
    curt_kwh.append(row_kwh)
    curt_aud.append(row_aud)
    at_cap.append(row_cap)

d["curtailment_heatmap"] = {
    "postcode_prefixes": postcodes,
    "hours": hours,
    "curtailed_kwh": curt_kwh,
    "curtailed_aud": curt_aud,
    "at_cap_share": at_cap,
    "total_curtailed_kwh": round(sum(sum(r) for r in curt_kwh), 3),
    "total_curtailed_aud": round(sum(sum(r) for r in curt_aud), 2),
    "price_unit": "$/kWh",
}

# Drop the legacy envelope_heatmap key
d.pop("envelope_heatmap", None)

d["_note"] = (
    "Sample fallback data for the dashboard when live aggregator data is unavailable. "
    "v0.5.0 shapes: price_response is split into import/export curves; envelope compliance "
    "has been replaced by a curtailment heatmap quantifying static-cap export losses."
)

with SAMPLE.open("w") as f:
    json.dump(d, f, indent=2)

print(f"Wrote {SAMPLE}")
print(f"  price_response regions: {list(d['price_response']['regions'].keys())}")
print(f"  curtailment total kWh: {d['curtailment_heatmap']['total_curtailed_kwh']}")
print(f"  curtailment total $: {d['curtailment_heatmap']['total_curtailed_aud']}")
