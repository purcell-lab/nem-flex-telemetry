# NEM Flex Telemetry

**OpenNEM for the demand side. A community-built, open-data picture of household demand flexibility in the National Electricity Market.**

[![Build](https://img.shields.io/github/actions/workflow/status/purcell-lab/nem-flex-telemetry/aggregate.yml?label=aggregation&style=flat-square)](https://github.com/purcell-lab/nem-flex-telemetry/actions/workflows/aggregate.yml)
[![Last data update](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpurcell-lab.github.io%2Fnem-flex-telemetry%2Fdata%2Fstatus.json&query=%24.last_updated&label=last+data+update&style=flat-square&color=4FB3BF)](https://purcell-lab.github.io/nem-flex-telemetry/)
[![Cohort size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fpurcell-lab.github.io%2Fnem-flex-telemetry%2Fdata%2Fstatus.json&query=%24.cohort_size&label=cohort+size&suffix=+households&style=flat-square&color=E8B254)](https://purcell-lab.github.io/nem-flex-telemetry/)
[![License: MIT](https://img.shields.io/badge/code-MIT-blue?style=flat-square)](LICENSE-CODE)
[![License: CC BY 4.0](https://img.shields.io/badge/data-CC--BY--4.0-lightgreen?style=flat-square)](LICENSE-DATA)

---

## Why this exists

AEMO's Integrating Price-Responsive Resources (IPRR) program asks four questions that no existing dataset answers:

1. How much flexible load exists in the NEM right now, at the distribution level?
2. Do households actually respond to price signals, and by how much?
3. What are the real envelope constraints (import/export limits) in each postcode?
4. What is the counterfactual cost of demand that was not deferred?

Distributed optimisers like [HAEO](https://github.com/hass-energy/haeo) already answer these questions inside each household. This project makes those answers visible at cohort scale, with open data and no central intermediary.

Frame: "IPRR Reporting Track, community data layer."

---

## The 12-field schema

Every 5-minute interval, each household pushes one record. Full specification: [SCHEMA.md](SCHEMA.md).

| Field | Type | Units | Description |
|---|---|---|---|
| `interval_start_utc` | string | ISO8601 | Interval start, UTC |
| `region` | string | NEM code | NSW1, QLD1, VIC1, SA1, TAS1 |
| `postcode_prefix` | string | 3 digits | First 3 digits of postcode only |
| `net_import_kw` | float | kW | Signed: positive = importing, negative = exporting |
| `price_signal_seen` | float | $/MWh | RRP or tariff signal used by optimiser |
| `optimiser_setpoint_kw` | float | kW | Setpoint commanded by HAEO |
| `flex_available_up_kw` | float | kW | Headroom available to increase load |
| `flex_available_down_kw` | float | kW | Headroom available to decrease load / export |
| `storage_soc_pct` | float | % | Battery state of charge, 0-100 |
| `envelope_import_limit_kw` | float | kW | DNSP-derived import limit |
| `envelope_export_limit_kw` | float | kW | DNSP-derived export limit |
| `naive_baseline_kw` | float | kW | What consumption would have been without optimisation |

The `naive_baseline_kw` field is the receipts field: it lets auditors verify demand response ex-post without trusting any single party's measurement. The `price_signal_seen` field makes the demand-response curve estimable for the first time from open community data.

---

## How to join (4 steps)

**Prerequisites:** Home Assistant running with [HAEO](https://github.com/hass-energy/haeo) configured.

### Step 1: Create a fine-grained GitHub PAT

In your GitHub account, create a fine-grained PAT scoped to `purcell-lab/nem-flex-telemetry` with `Contents: Read and write` permission only. Copy the token.

### Step 2: Add the HACS custom repository

In HACS, go to Integrations, click the three-dot menu, select "Custom repositories", paste `https://github.com/purcell-lab/nem-flex-telemetry`, category Integration.

### Step 3: Install the integration

Search HACS for "NEM Flex Telemetry" and install. Restart Home Assistant.

### Step 4: Run the config flow

Settings > Devices and Services > Add Integration > NEM Flex Telemetry. Follow the prompts: enter your PAT, household ID slug, postcode prefix (3 digits), NEM region, map your HAEO entities, and opt in to cohort participation and the CC-BY-4.0 data licence.

Full guide: [docs/INSTALL.md](docs/INSTALL.md)

---

## Privacy posture

- Postcode prefix (first 3 digits) only. No exact location. No NMI. No appliance-level data.
- Data is published under CC-BY-4.0. You retain no expectation of re-identification given the postcode prefix anonymisation.
- You can withdraw at any time by deleting your branch and raising an issue to request data removal from historical cohort files.

---

## Architecture

```mermaid
graph LR
    HA[Home Assistant\n+ HAEO] -->|5-min state reads| INT[nem_flex_telemetry\nintegration]
    INT -->|hourly JSONL commit\nPyGithub + fine-grained PAT| RAW[data/raw/household_id/\nYYYY/MM/DD.jsonl]
    RAW -->|push trigger\n+ hourly cron| AGG[aggregate.yml\nGitHub Action]
    AGG -->|pandas + pyarrow| PAR[data/cohort/\ndaily hourly 5min\n.parquet]
    AGG -->|derived JSON views| SITEDATA[site/data/*.json]
    SITEDATA -->|GitHub Pages| DASH[Dashboard\nObservable Plot]
```

---

## Roadmap

| Version | Milestone |
|---|---|
| v0.1 | Single-household MVP: integration installs, pushes JSONL, dashboard renders |
| v0.2 | Cohort of 10 households across at least 2 NEM regions |
| v0.3 | ARENA-grant-ready cohort of 100, automated IPRR-format reporting exports |
| v1.0 | IPRR-submission-ready cohort of 1000, full price-response curve with statistical confidence |

---

## Acknowledgements

- [HAEO](https://github.com/hass-energy/haeo) by the hass-energy team: the LP optimiser this integration reads.
- [EMHASS](https://github.com/davidusb-geek/emhass): complementary open-source energy management.
- [OpenElectricity](https://openelectricity.org.au/) (formerly OpenNEM): the generation-side open-data standard this project mirrors for demand.
- [Project Edith](https://www.ausgrid.com.au/-/media/Documents/1-PDF/Project-Edith-Insights-Report-Dec-2025.pdf): Ausgrid's locational DER trial, evidence base for envelope constraints.
- AEMO IPRR team: for the [HLIA framework](https://www.aemo.com.au/-/media/files/initiatives/integrating-price-responsive-resources-into-the-nem/iprr---hlia---v11-for-publication.pdf) and [Price Responsive Reporting Guidelines](https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/unscheduled-price-responsive-resources/final-determination-price-responsive-reporting-guidelines.pdf).

---

## References

- AEMO IPRR HLIA v1.1: https://www.aemo.com.au/-/media/files/initiatives/integrating-price-responsive-resources-into-the-nem/iprr---hlia---v11-for-publication.pdf
- AEMO Price Responsive Reporting Guidelines (Final): https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/unscheduled-price-responsive-resources/final-determination-price-responsive-reporting-guidelines.pdf
- Project Edith Stage 3 Insights Report (Dec 2025): https://www.ausgrid.com.au/-/media/Documents/1-PDF/Project-Edith-Insights-Report-Dec-2025.pdf
- OpenElectricity: https://openelectricity.org.au/
- HAEO: https://github.com/hass-energy/haeo

---

Code: MIT. Data: CC-BY-4.0. See [LICENSE-CODE](LICENSE-CODE) and [LICENSE-DATA](LICENSE-DATA).
