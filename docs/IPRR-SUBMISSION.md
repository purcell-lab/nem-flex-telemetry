# Draft submission: AEMO IPRR Reporting Track consultation

**To:** NEMReform@aemo.com.au
**Re:** Integrating Price-Responsive Resources, Reporting Track, open community data layer
**From:** Mark Purcell, Sunshine Coast QLD

---

AEMO's Price Responsive Reporting Guidelines (Final Determination, 2024) establish a Reporting Track for unscheduled price-responsive resources. The HLIA (v1.1) defines the High-Level Integration Architecture for these resources. Both documents assume the reporting pipeline runs from aggregator to AEMO, with households as passive endpoints.

I am writing to propose a complementary layer: open community telemetry, published directly by households under a standard schema, with no central intermediary.

**The gap the Reporting Track does not close**

The Guidelines require aggregators to report available capacity, price responsiveness, and envelope constraints. This produces regulatory compliance data, not open data. No researcher, network planner, or competing aggregator can see the underlying distribution of household flexibility. The demand side remains opaque at the granularity that matters for network planning.

OpenElectricity (openelectricity.org.au) solved this problem for generation in 2015. There is no generation-equivalent for demand. This project is a direct attempt to build it.

**NEM Flex Telemetry**

NEM Flex Telemetry (github.com/purcell-lab/nem-flex-telemetry) is an open-source Home Assistant integration that reads state from HAEO (github.com/hass-energy/haeo), an open LP-based energy optimiser, and pushes 5-minute interval telemetry to a public GitHub repository. The data is published under CC-BY-4.0.

The 12-field schema captures exactly what the Reporting Guidelines require from aggregators, at household scale, with privacy preserved at postcode-prefix (3-digit) resolution. The four fields that have previously been unmeasurable at open-data scale are:

1. `flex_available_up_kw` and `flex_available_down_kw`: the real-time flexible capacity stack, not a modelled estimate.
2. `price_signal_seen`: the price or tariff signal the household's optimiser actually used. Pairing this with `net_import_kw` across a cohort yields a direct empirical estimate of price elasticity of residential demand. This has not been possible from open data before.
3. `envelope_import_limit_kw` and `envelope_export_limit_kw`: the actual DNSP or firmware constraints active at the household. Project Edith (Ausgrid, Dec 2025, p.14) identifies the absence of this data as a critical gap in locational DER planning. This schema collects it at scale.
4. `naive_baseline_kw`: the counterfactual consumption estimate. This is the receipts field. It makes demand response auditable by any third party without trusting any single reporting entity's measurement.

**Why open data matters here**

The Reporting Track produces compliance data. What the NEM also needs is evidence for policy design: how much flexibility actually exists at each postcode prefix, how it responds to price at different RRP levels, and whether DNSP export constraints are binding. These questions require a dataset that is open, granular, and growing over time. Compliance reporting does not produce that dataset.

The HLIA (v1.1, s.3.2) notes that AEMO intends to publish aggregated demand-side data. I welcome that. But aggregated data published by AEMO is not a substitute for a community-maintained raw dataset that researchers, network planners, and policy teams can query directly.

**Proposed relationship to the Reporting Track**

This project does not seek to replace the Reporting Track. It is a complement:

- Households that choose to participate in an aggregation scheme under the Reporting Track can continue to do so. The NEM Flex Telemetry schema does not conflict with any aggregator's data model.
- The open dataset provides a baseline against which aggregator-reported capacity can be benchmarked.
- The price-response scatter (price_signal_seen vs net_import_kw across a cohort) provides the first open empirical estimate of residential demand elasticity, directly informing AEMO's load forecasting and the calibration of any demand-response dispatch model.

**What AEMO could do**

1. Recognise NEM Flex Telemetry as a candidate community-data input for the IPRR program. This requires no regulatory change, only a statement that the schema is compatible with IPRR reporting concepts.
2. Publish the IPRR HLIA as a machine-readable schema (JSON Schema or similar) to allow community projects to align their data models formally.
3. Consider a pathway for community dataset contributions to inform AEMO's planning work, equivalent to how OpenElectricity data is routinely cited in AEMO publications.

**Technical readiness**

The integration is at v0.1.0 (single-household MVP). The schema is stable. The GitHub Actions aggregation pipeline is live. The dashboard renders out-of-the-box with synthetic sample data and will update automatically as households join.

The roadmap to 1000 households (v1.0) is realistic given the existing HAEO user base and the low friction of the HACS installation path. A cohort of that size, across multiple NEM regions, would provide statistically significant price-response curves and the first open envelope constraint dataset in the NEM.

I am happy to provide a technical briefing on the schema and pipeline architecture at AEMO's request.

---

**References**

- AEMO IPRR HLIA v1.1: https://www.aemo.com.au/-/media/files/initiatives/integrating-price-responsive-resources-into-the-nem/iprr---hlia---v11-for-publication.pdf
- AEMO Price Responsive Reporting Guidelines (Final Determination): https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/unscheduled-price-responsive-resources/final-determination-price-responsive-reporting-guidelines.pdf
- Project Edith Stage 3 Insights Report (Ausgrid, Dec 2025): https://www.ausgrid.com.au/-/media/Documents/1-PDF/Project-Edith-Insights-Report-Dec-2025.pdf
- NEM Flex Telemetry: https://github.com/purcell-lab/nem-flex-telemetry
- HAEO: https://github.com/hass-energy/haeo
- OpenElectricity: https://openelectricity.org.au/
