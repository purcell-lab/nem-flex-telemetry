# NEM Flex Telemetry: Schema Specification v1.1

This document is the authoritative specification for the 13-field telemetry record pushed by each participating household every 5 minutes.

Machine-readable JSON Schema: [schema/telemetry.schema.json](schema/telemetry.schema.json)

---

## Rationale

The NEM has detailed generation telemetry at 5-minute resolution. The demand side has almost nothing comparable. AEMO's IPRR Reporting Track (HLIA v1.1, Price Responsive Reporting Guidelines) requires reporting on:

- Available flexible capacity (up and down)
- Price responsiveness
- Envelope constraints (import/export limits set by DNSP or inverter firmware)
- Counterfactual baseline (what would have happened without the optimiser)

This 13-field schema captures all four. It is intentionally minimal: no appliance-level data, no NMI, no exact location.

Buy/sell asymmetry is increasingly load-bearing as middle-of-day feed-in tariffs collapse or go negative. Publishing both prices the optimiser saw makes this visible at cohort scale for the first time.

---

## Fields

### `interval_start_utc`

- **Type:** string
- **Format:** ISO8601, UTC only (e.g. `2025-06-01T03:30:00Z`)
- **Units:** N/A
- **Required:** yes
- **Notes:** Always UTC. The interval covers `[interval_start_utc, interval_start_utc + 5 min)`. Do not use local time.

### `region`

- **Type:** string
- **Enum:** `NSW1`, `QLD1`, `VIC1`, `SA1`, `TAS1`
- **Required:** yes
- **Notes:** NEM dispatch region code as used by AEMO NEMDE. Set once in config, not per-interval.

### `postcode_prefix`

- **Type:** string
- **Pattern:** `^[0-9]{3}$`
- **Required:** yes
- **Notes:** First 3 digits of the household's postcode only. This is the privacy boundary. A 3-digit prefix covers a geographic area large enough to prevent re-identification in all Australian metro and most regional contexts.

### `net_import_kw`

- **Type:** number
- **Units:** kW, signed
- **Sign convention:** positive = net import from grid, negative = net export to grid
- **Required:** yes
- **Entity (primary):** `sensor.grid_active_power`
- **Notes:** Measured at the grid connection point (meter boundary). This is the field AEMO's demand forecasting cares about.

### `price_signal_seen`

- **Type:** number
- **Units:** $/MWh
- **Required:** yes
- **Entity (primary):** `number.grid_import_price` ($/kWh, converted to $/MWh on push)
- **Notes:** The buy price or tariff signal the optimiser used as its import objective function input for this interval. May be spot RRP (via Amber Electric, LocalVolts, or similar), a TOU tariff band midpoint, or a broker-provided signal. This field is what makes the demand-response curve estimable from open data: pairing `net_import_kw` with `price_signal_seen` across a cohort allows researchers to fit a price-elasticity curve for residential demand flexibility without a controlled experiment.

### `price_export_seen`

- **Type:** number
- **Units:** $/MWh, signed
- **Sign convention:** positive = paid for exports (household earns), negative = paying to export (negative feed-in tariff event)
- **Required:** yes (schema v1.1+)
- **Entity (primary):** `number.grid_export_price` ($/kWh, converted to $/MWh on push)
- **Notes:** Captures the asymmetry between buy and sell price seen by the optimiser. Critical for accurate counterfactual computation when the feed-in tariff (FiT) differs from the import price, including negative-FiT events. Without this field, cohort-level savings calculations that assume symmetry systematically overstate benefits during low or negative FiT periods.

### `optimiser_setpoint_kw`

- **Type:** number
- **Units:** kW, signed
- **Sign convention:** same as `net_import_kw`
- **Required:** yes
- **Entity (primary):** `sensor.battery_active_power`
- **Notes:** The setpoint commanded by the optimiser for this interval. Distinct from `net_import_kw` because uncontrollable loads and generation will cause divergence. The gap between setpoint and actuals is a residual uncontrollable load signal.

### `flex_available_up_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Entity (primary):** not always directly exposed by HAEO; see derivation note.
- **Notes:** Upward flexibility available: additional load the household could absorb right now (charge battery, run hot water, pre-cool). If HAEO exposes `sensor.haeo_flex_up` or `sensor.haeo_available_charge_power`, those are used directly. If not, the integration derives this from battery max charge rate minus the current battery setpoint. Aggregator entities (deferrable loads, hot water service, EV charge headroom) are not yet included; cohort v0.3 will integrate them.

### `flex_available_down_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Entity (primary):** not always directly exposed by HAEO; see derivation note.
- **Notes:** Downward flexibility available: load reduction or export increase deliverable right now. Derived analogously to `flex_available_up_kw` from battery max discharge rate minus current discharge rate when HAEO does not expose it directly. This is the field most relevant to emergency demand response events.

### `storage_soc_pct`

- **Type:** number
- **Units:** percent, 0-100 inclusive
- **Required:** yes
- **Entity (primary):** `sensor.battery_state_of_charge`
- **Notes:** Battery state of charge. Report 0 if no battery is present (not null, to preserve schema uniformity). Used to model the aggregate stored energy available to the cohort.

### `envelope_import_limit_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Entity (primary):** `number.grid_import_limit`
- **Notes:** The maximum import the household is allowed at this moment, as tracked by the optimiser. May originate from: DNSP export management scheme (CSIP-AUS), network constraint, inverter firmware limit, or household tariff structure. Reporting this at cohort scale provides the first open dataset of real envelope constraints, which Project Edith (Ausgrid, Dec 2025) identified as a critical missing input for locational DER planning.

### `envelope_export_limit_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Entity (primary):** `number.grid_export_limit`
- **Notes:** The maximum export allowed at this moment. Stored as a positive kW magnitude. The entity on some HAEO instances returns this signed negative for display purposes; the integration takes the absolute value before pushing.

### `naive_baseline_kw`

- **Type:** number
- **Units:** kW, signed (same convention as `net_import_kw`)
- **Required:** yes
- **Entity (primary):** `sensor.load_power`
- **Notes:** The `naive_baseline_kw` is the receipts field. It captures what the household would have consumed if the optimiser had not been active. Where HAEO exposes `sensor.haeo_naive_baseline` (a physics-based LP counterfactual), that is preferred. Otherwise `sensor.load_power` is used as a conservative estimate (realised load as proxy baseline). Enables ex-post verification of demand response without trusting any single party's measurement.

The counterfactual savings formula (asymmetric, using correct price per side) is:

```
savings_$ per interval =
  if net_import_kw > 0 (net importer):
    (naive_baseline_kw - net_import_kw) * price_signal_seen / 1000 / 12
  else (net exporter):
    (naive_baseline_kw - net_import_kw) * price_export_seen / 1000 / 12
```

The `/1000` converts kW to MW (for $/MWh units). The `/12` converts from per-hour to per-5-minute interval.

---

## Sample valid JSONL record (schema v1.1)

```json
{"interval_start_utc":"2025-06-01T03:30:00Z","region":"QLD1","postcode_prefix":"456","net_import_kw":1.42,"price_signal_seen":87.50,"price_export_seen":52.00,"optimiser_setpoint_kw":1.50,"flex_available_up_kw":2.10,"flex_available_down_kw":4.30,"storage_soc_pct":72.5,"envelope_import_limit_kw":5.0,"envelope_export_limit_kw":5.0,"naive_baseline_kw":3.85}
```

---

## Storage format

Records are stored as newline-delimited JSON (JSONL) at:

```
data/raw/<household_id>/YYYY/MM/DD.jsonl
```

Where `household_id` is the slug entered during config flow (e.g. `sunshine-coast-01`). One file per day. Records are appended in interval order. Duplicate intervals are deduplicated by the aggregation action (last-write wins).

---

## Cohort aggregation outputs

The aggregation action produces:

- `data/cohort/5min/YYYY/MM/DD.parquet`: all households, 5-minute resolution
- `data/cohort/hourly/YYYY/MM/DD.parquet`: resampled to 1-hour intervals
- `data/cohort/daily/YYYY/MM/DD.parquet`: daily summaries

---

## Privacy and governance

1. No NMI, no meter ID, no exact address, no GPS coordinates, no appliance-level data.
2. Postcode prefix (3 digits) is the maximum geographic resolution.
3. All data published under CC-BY-4.0. Attribution: "NEM Flex Telemetry contributors, https://github.com/purcell-lab/nem-flex-telemetry".
4. Schema changes follow the RFC process in [CONTRIBUTING.md](CONTRIBUTING.md). Breaking changes require a version bump and migration guide.
5. Any household may request deletion of their data by raising a GitHub issue. Cohort files will be re-generated excluding their records within 5 business days.

---

## Schema version history

| Version | Changes |
|---|---|
| 0.1.0 | Initial 12-field schema |
| 1.1 | Added `price_export_seen` (13th field). Additive, non-breaking for new pushes. Old records without this field fail schema validation and are excluded from cohort aggregation unless backfilled. |

---

## Validators

- JSON Schema (jsonschema Python library): [schema/telemetry.schema.json](schema/telemetry.schema.json)
- CI validation runs on every PR touching `data/raw/**` via [.github/workflows/validate.yml](.github/workflows/validate.yml)
- Integration-side validation runs before every push in `coordinator.py` using voluptuous

---

## Relationship to AEMO IPRR

The HLIA (v1.1) defines "High-Level Integration Architecture" for price-responsive resources. The Price Responsive Reporting Guidelines (Final Determination) require reporting of available capacity, price responsiveness, and envelope constraints. This schema maps directly to those requirements as a community data complement. See [docs/IPRR-SUBMISSION.md](docs/IPRR-SUBMISSION.md) for the full framing.
