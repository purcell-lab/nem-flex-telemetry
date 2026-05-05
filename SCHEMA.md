# NEM Flex Telemetry: Schema Specification v0.1

This document is the authoritative specification for the 12-field telemetry record pushed by each participating household every 5 minutes.

Machine-readable JSON Schema: [schema/telemetry.schema.json](schema/telemetry.schema.json)

---

## Rationale

The NEM has detailed generation telemetry at 5-minute resolution. The demand side has almost nothing comparable. AEMO's IPRR Reporting Track (HLIA v1.1, Price Responsive Reporting Guidelines) requires reporting on:

- Available flexible capacity (up and down)
- Price responsiveness
- Envelope constraints (import/export limits set by DNSP or inverter firmware)
- Counterfactual baseline (what would have happened without the optimiser)

This 12-field schema captures all four. It is intentionally minimal: no appliance-level data, no NMI, no exact location.

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
- **Notes:** Measured at the grid connection point (meter boundary). This is the field AEMO's demand forecasting cares about.

### `price_signal_seen`

- **Type:** number
- **Units:** $/MWh
- **Required:** yes
- **Notes:** The price or tariff signal that the HAEO optimiser used as its objective function input for this interval. May be spot RRP (via Amber Electric or similar), a time-of-use tariff band midpoint, or a broker-provided signal. This field is what makes the demand-response curve estimable from open data for the first time: by pairing `net_import_kw` with `price_signal_seen` across a cohort, researchers can fit a price-elasticity curve for residential demand flexibility without any controlled experiment.

### `optimiser_setpoint_kw`

- **Type:** number
- **Units:** kW, signed
- **Sign convention:** same as `net_import_kw`
- **Required:** yes
- **Notes:** The setpoint commanded by HAEO for this interval. Distinct from `net_import_kw` because uncontrollable loads and generation will cause divergence. The gap between setpoint and actuals is a residual uncontrollable load signal.

### `flex_available_up_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Notes:** Upward flexibility available: the additional load the household could absorb right now (charge battery, run hot water, pre-cool). Computed by HAEO as the headroom between current setpoint and the import envelope limit. Must be >= 0.

### `flex_available_down_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Notes:** Downward flexibility available: the load reduction or export increase the household could deliver right now. Computed by HAEO as the headroom between current setpoint and the export envelope limit. Must be >= 0. This is the field most relevant to emergency demand response events.

### `storage_soc_pct`

- **Type:** number
- **Units:** percent, 0-100 inclusive
- **Required:** yes
- **Notes:** Battery state of charge. Report 0 if no battery is present (not null, to preserve schema uniformity). Used to model the aggregate stored energy available to the cohort.

### `envelope_import_limit_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Notes:** The maximum import the household is allowed at this moment, as tracked by HAEO. This limit may originate from: DNSP export management scheme, network constraint, inverter firmware limit, or household tariff structure. Reporting this at cohort scale provides the first open dataset of real envelope constraints, which Project Edith (Ausgrid, Dec 2025) identified as a critical missing input for locational DER planning.

### `envelope_export_limit_kw`

- **Type:** number
- **Units:** kW, non-negative
- **Required:** yes
- **Notes:** The maximum export allowed at this moment. See `envelope_import_limit_kw` for sourcing notes. Positive number representing the magnitude of the allowed export flow.

### `naive_baseline_kw`

- **Type:** number
- **Units:** kW, signed (same convention as `net_import_kw`)
- **Required:** yes
- **Notes:** The `naive_baseline_kw` is the receipts field. It captures what the household would have consumed (or exported) if HAEO had not optimised: a 7-day same-period rolling average of `net_import_kw` prior to the optimiser being active, or a physics-based estimate from HAEO's load model. This field enables ex-post verification of demand response without trusting any single party's measurement. Auditors can compare `naive_baseline_kw` against `net_import_kw` to estimate the actual load shift delivered. The counterfactual savings in dollars are: `sum((naive_baseline_kw - net_import_kw) * price_signal_seen / 1000 / 12)` per 5-minute interval.

---

## Sample valid JSONL record

```json
{"interval_start_utc":"2025-06-01T03:30:00Z","region":"QLD1","postcode_prefix":"456","net_import_kw":1.42,"price_signal_seen":87.50,"optimiser_setpoint_kw":1.50,"flex_available_up_kw":2.10,"flex_available_down_kw":4.30,"storage_soc_pct":72.5,"envelope_import_limit_kw":5.0,"envelope_export_limit_kw":5.0,"naive_baseline_kw":3.85}
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

## Validators

- JSON Schema (jsonschema Python library): [schema/telemetry.schema.json](schema/telemetry.schema.json)
- CI validation runs on every PR touching `data/raw/**` via [.github/workflows/validate.yml](.github/workflows/validate.yml)
- Integration-side validation runs before every push in `coordinator.py` using voluptuous

---

## Relationship to AEMO IPRR

The HLIA (v1.1) defines "High-Level Integration Architecture" for price-responsive resources. The Price Responsive Reporting Guidelines (Final Determination) require reporting of available capacity, price responsiveness, and envelope constraints. This schema maps directly to those requirements as a community data complement. See [docs/IPRR-SUBMISSION.md](docs/IPRR-SUBMISSION.md) for the full framing.
