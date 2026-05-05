# NEM Flex Telemetry: Schema Specification v2.0

This document is the authoritative specification for the telemetry record pushed by each participating household every 5 minutes.

Machine-readable JSON Schema: [schema/telemetry.schema.json](schema/telemetry.schema.json)

---

## Overview

Schema v2.0 introduces:
- 18 flat top-level fields (up from 13 in v1.1)
- `assets[]` array for per-asset records (home battery, EVs)
- `deferrable_loads[]` array (reserved for v0.4)
- All prices in $/kWh throughout (no $/MWh)
- HAEO shadow prices as first-class fields
- Solar generation and inflexible house load as distinct fields

No migration from v1.1 exists. No v1.1 records were published to production, so this is a clean break.

---

## Top-level fields (18 flat fields)

| Field | Type | Units | Required | Description |
|---|---|---|---|---|
| `schema_version` | string | | yes | Always `'2.0'`. Records with any other value are rejected. |
| `interval_start_utc` | string | ISO8601 | yes | Interval start, UTC. Covers [start, start + 5 min). |
| `region` | string | NEM code | yes | NSW1, QLD1, VIC1, SA1, TAS1 |
| `postcode_prefix` | string | 3 digits | yes | First 3 digits of postcode only. Privacy boundary. |
| `net_import_kw` | float | kW | yes | Signed: positive = importing, negative = exporting. |
| `solar_kw` | float | kW | yes | Solar generation (always >= 0). |
| `house_load_kw` | float | kW | yes | Inflexible base load (>= 0). total\_load minus deferrable current, clamped to 0. |
| `deferrable_load_kw` | float | kW | yes | Sum of deferrable load current\_kw (>= 0). Zero for v0.3. |
| `naive_baseline_kw` | float | kW | yes | Estimated consumption without optimisation. |
| `naive_baseline_method` | string | enum | yes | `'subtraction'` or `'haeo_counterfactual'`. |
| `price_signal_seen` | float | $/kWh | yes | Buy price seen by the optimiser. Range: -2.0 to 20.0. |
| `price_export_seen` | float | $/kWh | yes | Sell price seen by the optimiser. Positive = FiT earned; negative = negative-FiT event. |
| `envelope_import_limit_kw` | float | kW | yes | Maximum import allowed (>= 0). From `number.grid_import_limit`. |
| `envelope_export_limit_kw` | float | kW | yes | Maximum export allowed (>= 0). Positive magnitude. |
| `flex_available_up_kw` | float | kW | yes | Upward flexibility available (>= 0). |
| `flex_available_down_kw` | float | kW | yes | Downward flexibility available (>= 0). |
| `shadow_energy_price` | float or null | $/kWh | yes | Aggregate shadow energy price (LP dual cross-check). Null if no shadow sensors present. |
| `shadow_load_forecast_price` | float or null | $/kWh | yes | LP dual for load forecast constraint. From `sensor.load_forecast_limit_shadow_price`. |
| `shadow_solar_forecast_price` | float or null | $/kWh | yes | LP dual for solar forecast constraint. From `sensor.solar_forecast_limit_shadow_price`. |
| `shadow_envelope_import_price` | float or null | $/kWh | yes | LP dual for grid import envelope. From `sensor.grid_max_import_power_shadow_price`. Non-zero when envelope is binding. |
| `shadow_envelope_export_price` | float or null | $/kWh | yes | LP dual for grid export envelope. From `sensor.grid_max_export_power_shadow_price`. Non-zero when envelope is binding. |

**Price range:** All price fields use $/kWh with minimum -2.0 and maximum 20.0. This range covers extreme negative-FiT events at the low end and capacity-price spikes at the high end.

---

## Arrays

### `assets[]`

Array of asset objects. Each participating household contributes records for their stationary battery and any EVs.

**Common fields (all asset kinds):**

| Field | Type | Units | Description |
|---|---|---|---|
| `asset_id` | string | | Slug: `'home_battery'`, `'ev1'`, `'ev2'`, etc. |
| `kind` | string | enum | `'stationary_battery'` or `'ev'` |
| `bidirectional_capable` | bool | | Whether hardware supports bidirectional flow. |
| `capacity_kwh` | float | kWh | Usable energy capacity. |
| `soc_pct` | float | % | State of charge, 0-100. |
| `setpoint_kw` | float or null | kW | Signed setpoint: positive = charging, negative = discharging. Null if entity unavailable. |
| `available_up_kw` | float | kW | Available upward flexibility from this asset. |
| `available_down_kw` | float | kW | Available downward flexibility from this asset. |
| `shadow_power_balance_price` | float or null | $/kWh | HAEO LP dual for this asset's power balance constraint. Null when asset is unavailable or unplugged. |

**EV-only fields (present when `kind == 'ev'`):**

| Field | Type | Description |
|---|---|---|
| `connection_state` | string enum | Inferred state: `'unplugged'`, `'plugged_idle'`, `'charging'`, `'discharging'`, `'driving'`. |
| `power_flow_capability` | string enum | Inferred capability: `'none'`, `'charge_only'`, `'bidirectional'`. |
| `departure_target_pct` | int or null | Target SOC at departure. Null if not configured. |
| `departure_time_utc` | string or null | Scheduled departure (ISO8601 UTC). Null if not configured. |

`additionalProperties` is `true` on asset objects so future kind-specific fields can be added without breaking v2.0 validation.

---

### `deferrable_loads[]`

Reserved for v0.4. Empty array for all v0.3 installs. Schema slot is kept so v0.4 agents can begin populating it without a schema version bump.

---

## Connection state inference

Plug state is inferred per-EV per-interval from shadow prices and setpoints. It is NOT read from any binary\_sensor entity (v0.3). Future versions may accept direct `binary_sensor` entities if households have them fitted.

Inference rules, applied in order:

1. If `shadow_power_balance_price` is null or unavailable, check SOC delta. If SOC falling rapidly (> 1.5% per 5min) with no shadow price: `'driving'`. Otherwise: `'unplugged'`.
2. If shadow is present AND setpoint is ~0 AND |SOC delta| < 0.5%: `'plugged_idle'`.
3. If setpoint > 0 kW OR SOC rising > 1% per 5min: `'charging'`.
4. If setpoint < 0 kW OR SOC falling > 1% per 5min: `'discharging'`.
5. Default (shadow present, no strong signal): `'plugged_idle'`.

Power flow capability inference:

- `'unplugged'` or `'driving'`: `'none'`
- Currently `'discharging'`: `'bidirectional'`. This EV is recorded as `last_bidirectional_ev_id`.
- Plugged AND this EV discharged within the last hour: `'bidirectional'` (sticky).
- Plugged AND no recent discharge observed: `'charge_only'` (conservative default; upgrades to `'bidirectional'` after first discharge observed).

The `last_bidirectional_ev_id` state is tracked in the coordinator across intervals so the labelling is stable within a session.

**Note:** Connection state is inferred, not measured. The `connection_state` field documents the inference result. Inference confidence is implicitly captured: `'driving'` requires both shadow unavailability and rapid SOC drop; `'discharging'` requires either a negative setpoint or rapid SOC fall.

---

## Shadow prices

Shadow prices are LP dual variables from the HAEO optimiser. They represent the marginal value of relaxing a binding constraint by one unit.

- `shadow_energy_price`: the marginal value of energy in the system balance ($/kWh). Non-zero when the overall energy balance is tight.
- `shadow_load_forecast_price`: marginal value of relaxing the load forecast limit ($/kWh).
- `shadow_solar_forecast_price`: marginal value of relaxing the solar forecast limit ($/kWh).
- `shadow_envelope_import_price`: marginal value of relaxing the import envelope ($/kWh). This is the dollar cost the DNSP constraint is imposing on the household per kWh. Non-zero = the envelope is actually binding during that interval.
- `shadow_envelope_export_price`: same for the export envelope.

Per-asset `shadow_power_balance_price`: the marginal value HAEO placed on one more kWh from that specific asset. Reflects SOC limits, battery degradation cost, or EV departure requirements being binding.

These are the unique fields in this schema: no other open-data source publishes household-level LP duals from a distributed energy optimiser. They allow researchers to infer: (a) which network constraints are actually binding in dollar terms; (b) how much the household values EV charge state at departure versus real-time energy arbitrage; (c) where forecasting errors are most costly (high load or solar forecast shadow = that forecast is driving dispatch decisions).

---

## Naive baseline method

| Value | Meaning |
|---|---|
| `'subtraction'` | `naive_baseline_kw = total_load_kw` (realised load as proxy). Conservative: will overstate savings when optimisation reduces apparent load. Entity: `sensor.load_power`. |
| `'haeo_counterfactual'` | LP no-flex branch counterfactual. More accurate but requires HAEO to expose the counterfactual sensor. Not available in all installs. |

---

## Counterfactual ledger formula (v2.0)

```
effective_price_kwh = price_signal_seen  if net_import_kw > 0
                    = price_export_seen  if net_import_kw <= 0

saving_aud = (naive_baseline_kw - net_import_kw) * effective_price_kwh * (300 / 3600)
```

The `(300 / 3600)` factor converts kW over a 5-minute interval to kWh. Prices are already in $/kWh. No `/1000` factor is needed.

---

## Sample valid JSONL record (schema v2.0)

```json
{"schema_version":"2.0","interval_start_utc":"2025-06-01T08:30:00Z","region":"QLD1","postcode_prefix":"456","net_import_kw":0.42,"solar_kw":4.8,"house_load_kw":1.2,"deferrable_load_kw":0.0,"naive_baseline_kw":2.8,"naive_baseline_method":"subtraction","price_signal_seen":0.0875,"price_export_seen":0.052,"envelope_import_limit_kw":5.0,"envelope_export_limit_kw":5.0,"flex_available_up_kw":2.1,"flex_available_down_kw":4.3,"shadow_energy_price":0.0,"shadow_load_forecast_price":0.0,"shadow_solar_forecast_price":0.0,"shadow_envelope_import_price":0.0,"shadow_envelope_export_price":0.0,"assets":[{"asset_id":"home_battery","kind":"stationary_battery","bidirectional_capable":true,"capacity_kwh":13.5,"soc_pct":72.5,"setpoint_kw":1.5,"available_up_kw":3.5,"available_down_kw":5.0,"shadow_power_balance_price":0.0}],"deferrable_loads":[]}
```

---

## Storage format

Records are stored as newline-delimited JSON (JSONL) at:

```
data/raw/<household_id>/YYYY/MM/DD.jsonl
```

One file per day. Records are appended in interval order. Duplicate intervals are deduplicated by the aggregation action (last-write wins).

---

## Cohort aggregation outputs

The aggregation action produces:

- `data/cohort/5min/YYYY/MM/DD.parquet`: all households, 5-minute resolution
- `data/cohort/hourly/YYYY/MM/DD.parquet`: resampled to 1-hour intervals
- `data/cohort/daily/YYYY/MM/DD.parquet`: daily summaries
- `site/data/assets_summary.json`: asset mix, V2G duty cycle, dispatch share (dashboard tab 6)
- `site/data/shadow_prices.json`: shadow price distributions and envelope heatmap (dashboard tab 7)

---

## Privacy and governance

1. No NMI, no meter ID, no exact address, no GPS coordinates, no appliance-level data.
2. Postcode prefix (3 digits) is the maximum geographic resolution.
3. All data published under CC-BY-4.0. Attribution: "NEM Flex Telemetry contributors, https://github.com/purcell-lab/nem-flex-telemetry".
4. Schema changes follow the RFC process in [CONTRIBUTING.md](CONTRIBUTING.md). Breaking changes require a version bump.
5. Any household may request deletion of their data by raising a GitHub issue. Cohort files will be re-generated excluding their records within 5 business days.

---

## Schema version history

| Version | Changes | Migration |
|---|---|---|
| 0.1.0 | Initial 12-field schema | N/A |
| 1.1 | Added `price_export_seen` (13th field). Additive, non-breaking. | Old records backfilled with `price_export_seen: 0.0` for aggregation. |
| 2.0 | 18 flat fields + `assets[]` + `deferrable_loads[]`. $/kWh throughout. HAEO shadow prices. Solar and house load split. `schema_version` field added. | Clean break. No v1.1 production records exist. v1.x records are rejected by CI. |

---

## Validators

- JSON Schema: [schema/telemetry.schema.json](schema/telemetry.schema.json)
- CI validation: [.github/workflows/validate.yml](.github/workflows/validate.yml). Checks schema\_version == '2.0', JSON Schema compliance, and $/kWh range (-2.0 to 20.0) on all price fields.
- Integration-side validation: `coordinator.py` runs voluptuous validation before every push.

---

## Relationship to AEMO IPRR

The HLIA (v1.1) defines the High-Level Integration Architecture for price-responsive resources. The Price Responsive Reporting Guidelines (Final Determination) require reporting of available capacity, price responsiveness, and envelope constraints. This schema maps directly to those requirements, with the addition of shadow prices as the marginal-value layer that AEMO's demand forecasting needs but cannot currently observe.

See [docs/IPRR-SUBMISSION.md](docs/IPRR-SUBMISSION.md) for the full framing.
