# Draft submission: AEMO IPRR Reporting Track consultation

**To:** NEMReform@aemo.com.au
**Re:** Integrating Price-Responsive Resources, Reporting Track, open community data layer
**From:** Mark Purcell, Sunshine Coast QLD

---

AEMO's Price Responsive Reporting Guidelines (Final Determination, 2024) establish a Reporting Track for unscheduled price-responsive resources. The HLIA (v1.1) defines the High-Level Integration Architecture for these resources. Both documents assume the reporting pipeline runs from aggregator to AEMO, with households as passive endpoints.

I am writing to propose a complementary layer: open community telemetry, published directly by households under a standard schema, with no central intermediary.

**The gap the Reporting Track does not close**

The Guidelines require aggregators to report available capacity, price responsiveness, and envelope constraints. This produces regulatory compliance data, not open data. No researcher, network planner, or competing aggregator can see the underlying distribution of household flexibility. The demand side remains opaque at the granularity that matters for network planning.

OpenElectricity (openelectricity.org.au) solved this problem for generation in 2015. There is no equivalent for demand. This project is a direct attempt to build it.

**NEM Flex Telemetry**

NEM Flex Telemetry (github.com/purcell-lab/nem-flex-telemetry) is an open-source Home Assistant integration that reads state from HAEO (github.com/hass-energy/haeo), an open LP-based energy optimiser, and pushes 5-minute interval telemetry to a public GitHub repository. The data is published under CC-BY-4.0.

The schema is now at v2.0 (integration v0.3.0), with 18 flat top-level fields, an asset array for batteries and EVs, and shadow prices as first-class fields. The schema maps to all four categories of data the Reporting Guidelines require from aggregators:

1. **Available flexible capacity:** `flex_available_up_kw` and `flex_available_down_kw` at cohort level. Per-asset: `available_up_kw` and `available_down_kw` for each battery and EV. This is real-time capacity, not a modelled estimate.

2. **Price responsiveness:** `price_signal_seen` and `price_export_seen` (both in $/kWh) paired with `net_import_kw` across a cohort. Pairing these across many households yields a direct empirical estimate of the price elasticity of residential demand. This has not been possible from open data before.

3. **Envelope constraints:** `envelope_import_limit_kw` and `envelope_export_limit_kw`. Project Edith (Ausgrid, Dec 2025, p.14) identifies the absence of this data as a critical gap in locational DER planning. This schema collects it at scale.

4. **Counterfactual baseline:** `naive_baseline_kw` with `naive_baseline_method` specifying how it was derived. The counterfactual formula is asymmetric: `(naive_baseline_kw - net_import_kw) * effective_price_kwh * (interval_seconds / 3600)`, where `effective_price_kwh` selects the correct buy or sell price based on the direction of power flow. This makes demand response auditable by any third party.

**Shadow pricing: the unique-value field**

Schema v2.0 adds a layer that no other open-data source currently provides: LP dual variables from the household optimiser.

When HAEO solves its LP for each 5-minute interval, it computes shadow prices alongside the dispatch solution. These are:

- `shadow_envelope_import_price` ($/kWh): the marginal cost to the household's optimiser of the DNSP import constraint. Zero most of the time. Non-zero during peak load periods when the import envelope is actually constraining dispatch. This is the dollar term that measures whether DNSP constraints are biting, at household scale.

- `shadow_envelope_export_price` ($/kWh): the same for the export envelope. Non-zero during the 11am-2pm solar saturation periods when distributed PV exceeds feeder capacity. This field shows, in dollar terms, where distributed solar is being curtailed by network constraints.

- `shadow_power_balance_price` per asset ($/kWh): the marginal value of one more kWh from a specific battery or EV. Reflects whether departure constraints, SOC limits, or degradation cost assumptions are the binding factor in each interval.

Existing IPRR data infrastructure captures kWh and price separately. NEM Flex Telemetry adds the marginal value the household optimiser placed on each constraint, which is what AEMO needs to forecast price-responsive behaviour accurately.

A household with a high `shadow_envelope_import_price` will respond strongly to a network operator signal. A household with a near-zero shadow will not respond, even if spot prices spike. This distinction is invisible from metered energy flows alone. Publishing it at cohort scale, by postcode prefix and hour of day, provides the first open dataset of real constraint-binding behaviour in the NEM demand side.

**Why open data matters here**

The Reporting Track produces compliance data. What the NEM also needs is evidence for policy design: how much flexibility actually exists at each postcode prefix, how it responds to price at different RRP levels, and whether DNSP export constraints are binding. These questions require a dataset that is open, granular, and growing over time. Compliance reporting does not produce that dataset.

The HLIA (v1.1, s.3.2) notes that AEMO intends to publish aggregated demand-side data. Aggregated data published by AEMO is not a substitute for a community-maintained raw dataset that researchers, network planners, and policy teams can query directly.

**Asset-level V2G visibility**

Schema v2.0 introduces an `assets[]` array with per-asset records including EV connection state and power flow capability. The connection state is inferred from LP dual variables, not from a binary sensor: if an EV's `shadow_power_balance_price` is null, it is unplugged (or driving). If it is present and the setpoint is positive, it is charging. If negative, discharging.

This inference approach means the schema can capture V2G dispatch events without requiring households to install additional hardware or configure additional entities. The LP dual being non-null is a necessary condition for the charger being connected; the setpoint direction distinguishes charging from discharging. This has direct relevance to AEMO's interest in EV flexibility as a demand-response resource, given the projected growth of EVs in the NEM over the next decade.

**Proposed relationship to the Reporting Track**

This project does not seek to replace the Reporting Track. It is a complement:

- Households participating in an aggregation scheme under the Reporting Track can continue to do so. The NEM Flex Telemetry schema does not conflict with any aggregator's data model.
- The open dataset provides a baseline against which aggregator-reported capacity can be benchmarked.
- The price-response scatter across a cohort provides the first open empirical estimate of residential demand elasticity, directly informing AEMO's load forecasting.
- The shadow price layer reveals which constraints are actually binding and where, in dollar terms, without requiring AEMO to collect it through a regulatory channel.

**What AEMO could do**

1. Recognise NEM Flex Telemetry as a candidate community-data input for the IPRR program. This requires no regulatory change, only a statement that the schema is compatible with IPRR reporting concepts.
2. Publish the IPRR HLIA as a machine-readable schema (JSON Schema or similar) to allow community projects to align their data models formally.
3. Consider a pathway for community dataset contributions to inform AEMO's planning work, equivalent to how OpenElectricity data is routinely cited in AEMO publications.

**Technical readiness**

The integration is at v0.3.0. Schema v2.0 is stable. The GitHub Actions aggregation pipeline produces seven dashboard views including the new asset and shadow price tabs. The installation path is via HACS, which is the standard mechanism for community Home Assistant integrations.

The roadmap to 1000 households (v1.0) is realistic given the existing HAEO user base and the low friction of the HACS installation path. A cohort of that size, across multiple NEM regions, would provide statistically significant price-response curves, the first open envelope constraint dataset in the NEM, and the first open dataset of real constraint-binding shadow prices from distributed energy optimisers.

I am happy to provide a technical briefing on the schema and pipeline architecture at AEMO's request.

---

**References**

- AEMO IPRR HLIA v1.1: https://www.aemo.com.au/-/media/files/initiatives/integrating-price-responsive-resources-into-the-nem/iprr---hlia---v11-for-publication.pdf
- AEMO Price Responsive Reporting Guidelines (Final Determination): https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/unscheduled-price-responsive-resources/final-determination-price-responsive-reporting-guidelines.pdf
- Project Edith Stage 3 Insights Report (Ausgrid, Dec 2025): https://www.ausgrid.com.au/-/media/Documents/1-PDF/Project-Edith-Insights-Report-Dec-2025.pdf
- NEM Flex Telemetry: https://github.com/purcell-lab/nem-flex-telemetry
- HAEO: https://github.com/hass-energy/haeo
- OpenElectricity: https://openelectricity.org.au/
