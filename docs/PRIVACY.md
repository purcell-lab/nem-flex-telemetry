# Privacy and threat model

NEM Flex Telemetry collects behind-the-meter (BTM) flexibility telemetry
from real households and publishes it to a public, openly-licensed data
repository. The whole point of the project is to make this data visible
to researchers, AEMO, networks and the policy community. That public
posture creates real privacy obligations, and this document explains how
the project handles them today and how that will tighten as the cohort
grows.

This document is a living artefact. If you spot a gap or disagree with a
trade-off, raise an issue.

## TL;DR

- The published data set contains **no name, no street address, no
  email, no NMI, no appliance-level breakdown**.
- Every household is identified only by an **anonymous identifier**
  (default: a randomly generated UUID v4, generated locally during
  setup) and a **postcode prefix** (the first three digits of the
  postcode only).
- During the **single-household and small-cohort phase (v0.1 to v0.4)**
  every commit to the public repository is signed by the contributing
  user's own GitHub identity. That **is a real attribution leak** and
  it is described in detail below. It is acceptable while the cohort is
  small and self-selected, and unacceptable for a production cohort.
- The aggregator enforces a **configurable k-anonymity threshold** on
  every per-region view and on the published parquet, and drops
  `household_id` from the parquet. The region threshold currently
  defaults to **k = 1 (no suppression)** because the cohort is two
  households in two regions, so **every per-region chart on the
  dashboard today is one household's data**. The dashboard labels those
  series as individual data rather than hiding the fact. See
  [k-anonymity guardrails](#k-anonymity-guardrails).
- **Raw per-household JSONL in `data/raw/` is public** under CC-BY-4.0
  until the v0.5 relay architecture lands. The guardrails apply to the
  derived outputs only; they do not protect the raw records.
- For the **production cohort (v0.5 and later)** the project moves to a
  relay architecture so that household → GitHub identity links no
  longer exist on the public record.

If your threat model includes a motivated adversary willing to correlate
fine-grained smart-meter data against utility records or commercial
data brokers, you should not participate until v0.5 ships. If you are a
research participant who simply wants to contribute load-flexibility
data to the public commons, the current posture is reasonable and the
default UUID identifier gives you a meaningful pseudonymity guarantee.

## What is in the data

Each 5-minute snapshot record (schema v2.0) contains:

- A schema version, a UTC interval start timestamp, the NEM region
  (e.g. `QLD1`), the postcode prefix (3 digits), and an anonymous
  household identifier.
- Net import power, solar generation, total load, and a deferrable
  load estimate, all in kW.
- Buy and sell price signals seen by the household, both in $/kWh.
- Import and export envelope limits in kW.
- Up and down flexibility headroom in kW.
- Five HAEO LP shadow prices (energy, load forecast, solar forecast,
  envelope import, envelope export) in $/kW per dispatch interval.
- An `assets[]` array describing the household's batteries and EVs:
  per-asset capacity, state of charge, current setpoint, available
  flex headroom, V2G capability, departure target where known, and a
  per-asset shadow price.
- A `deferrable_loads[]` array (currently empty for v0.3.x; populated
  in v0.4 with HWS, pool, AC pre-cool and similar).

The schema explicitly excludes:

- Personal name, email address, phone number, street address, postcode
  beyond the 3-digit prefix.
- The full NMI or any retailer-issued meter identifier.
- Appliance-level disaggregation (fridge versus oven versus washing
  machine).
- Indoor occupancy or activity inference signals (motion sensors,
  presence detectors, etc.).
- Inverter serial numbers, MAC addresses, or any device identifiers
  that could be cross-referenced against retailer or installer
  databases.

Household identifiers and postcode prefixes are the only fields with
any potential to be re-identified, and even those are pseudonymous
rather than personal.

## Threat model

The realistic adversaries are, in roughly increasing order of capability:

1. **Casual observer browsing the public repo**. Wants to know who
   contributes data. Mitigation: anonymous household identifiers,
   postcode-prefix only.
2. **Curious researcher correlating across public records**. Tries to
   match published load curves against publicly available demographic
   data, retailer marketing data, or social-media-disclosed solar /
   battery installations. Mitigation: pseudonymous identifiers, no
   exact location. The k-anonymity guardrails on aggregated views only
   help once the cohort is large enough to raise the threshold, and the
   raw JSONL is public in the meantime.
3. **Motivated re-identification attacker with commercial smart-meter
   data**. Has access to NMI-level retailer data or AEMO MDFF data and
   wants to match it to a public NEM Flex Telemetry household.
   Mitigation: this is the hard case. 5-minute load curves at a single
   dwelling are essentially a fingerprint, and no anonymity scheme
   protects against it once an adversary has matching meter data.
   The only effective defence is to ensure that participation is
   strictly opt-in, fully informed, and that participants understand
   the residual risk. No technical control can fully neutralise this
   class of attacker.
4. **Adversary with access to GitHub**. Wants to enumerate
   participating households via commit metadata. **This is the
   adversary that the current Option 1 architecture does not defend
   against.** See the next section.

## Identity attribution: Option 1 direct commits (v0.1 to v0.4)

While the project is in pre-cohort development, the integration
authenticates each Home Assistant instance to GitHub via OAuth Device
Flow against the household user's own personal GitHub account, and
commits are pushed under that user's GitHub identity.

That choice has the following privacy property:

- Every commit in `data/raw/<anonymous-household-id>/...` is authored
  by, and visibly attributed to, the GitHub user who runs the
  integration on their Home Assistant instance.
- The mapping between anonymous household identifier and real GitHub
  username is therefore **public and permanent** in the commit history.
- A casual observer can read `git log` on the repository and see, for
  each anonymous household identifier, exactly which GitHub username
  contributed it. From there they can typically trace name, location
  hints, employer, and other public-profile information.
- This applies even though the household identifier itself is a
  random UUID. The pseudonymity is broken by the commit author field,
  not by the identifier.

We have chosen to accept this property during the early phase because:

- The cohort is small (one household at the time of writing) and every
  participant is a fully-informed contributor who already publishes
  under their own name in the energy policy community.
- Operating a relay during the prototype phase would slow iteration
  and divert effort from schema and dashboard work that has higher
  marginal value.
- The data published during this phase is exploratory and is not the
  basis of any regulatory submission. v0.5 onwards is the cohort
  phase, and that phase will not begin until the relay architecture
  is in place.

We do **not** consider this property acceptable for a production
cohort. The next section describes the path away from it.

## Planned relay architecture (v0.5 onwards)

Production cohort scaling requires breaking the link between household
contribution and personal GitHub identity. The plan is a thin relay
service that sits between household HA instances and the public
GitHub repository:

```
┌─────────────────────┐    HTTPS POST    ┌──────────┐    git push    ┌────────────────────┐
│ Home Assistant      │ ──────────────▶  │  Relay   │ ─────────────▶ │ GitHub repo        │
│ + integration       │  schema-validated│ (single  │  bot account   │ (single committer) │
│ (per-household key) │  payload         │  bot     │  identity      │                    │
└─────────────────────┘                  └──────────┘                └────────────────────┘
```

Properties of the relay design:

- The public GitHub repository sees only one committer identity
  (the project bot account). Per-household GitHub identities are no
  longer visible on commits.
- Each household's HA integration authenticates to the relay using a
  per-household key issued at signup, not their personal GitHub
  identity. The integration drops the GitHub Device Flow path.
- The relay is the only component that knows the mapping between
  per-household keys and any pre-existing identity material (such as
  the email address used at signup). That mapping is held in encrypted
  storage and is deletable on request.
- The relay enforces schema validation, rate-limiting (one snapshot
  per 5 minutes per household), and pricing range bounds before
  forwarding to GitHub. This protects the public dataset from
  malformed or adversarial submissions.
- The relay is small. A Cloudflare Worker, AWS Lambda, or single
  small VPS can serve a cohort of thousands at negligible cost.
- Source code for the relay will be published in a sibling repo so
  participants can audit it.

The integration will be updated to support the relay endpoint as a
new, preferred transport. The Device Flow GitHub transport will remain
available as an opt-in for users who explicitly want to keep
publishing under their own identity.

## Household identifier model

The household identifier is a **pseudonym**, not a true anonymous
token. It must be stable across snapshots from the same household so
that aggregations (e.g. a 7-day load profile) make sense, and that
stability is what makes it a pseudonym rather than a fresh anonymous
draw per record.

Properties:

- The integration generates a UUID v4 by default at install time,
  giving roughly 122 bits of entropy. Collision probability across
  a cohort of 1,000,000 households is below 1 in 10²⁴.
- Users may override the default with any non-empty string up to 128
  characters. Choosing a memorable label (e.g. `sunshine-coast-01`)
  is permitted but reduces the pseudonymity property. Users who do
  this are typically self-identified contributors who already
  associate themselves with the project publicly, so the choice is
  theirs to make.
- The identifier is stored locally in the Home Assistant config entry
  and is never re-derived from any device serial, MAC address, or
  user-entity field. Reinstalling the integration generates a new
  default identifier; users who want to preserve continuity can copy
  the previous identifier across.
- Once published, an identifier cannot be retroactively renamed. If a
  participant wants to break continuity (e.g. because they suspect
  re-identification), they should generate a new identifier and stop
  publishing under the old one. The old data remains in the repo
  unless a removal request is granted.
- Removal requests are accepted. The repository owner will, on
  request, scrub historical commits for a given identifier from the
  default branch and publish a corrected aggregate. This is best-effort
  given the immutability properties of git, and is documented in the
  withdrawal process.

## k-anonymity guardrails

This section describes what `scripts/aggregate.py` actually enforces.
The tests in `tests/test_k_anonymity.py` run in CI on every pull
request that touches the aggregator and fail if any published regional
series is built from fewer than `K_MIN_REGION` households.

### Thresholds

| Constant | Default | Environment override | Applies to |
|---|---|---|---|
| `K_MIN_REGION` | 1 | `NEM_FLEX_K_MIN_REGION` | Every per-region series and the cohort parquet |
| `K_MIN_PREFIX` | 5 | `NEM_FLEX_K_MIN_PREFIX` | `postcode_prefix` in the cohort parquet |
| `K_ADVISORY` | 5 | `NEM_FLEX_K_ADVISORY` | Dashboard labelling only |

**Why `K_MIN_REGION` is 1 today.** The cohort is two households, one
in NSW1 and one in QLD1. At k = 5 every per-region chart would be
suppressed and the dashboard would show nothing regional. The project
owner has not yet set a production threshold. Until then the default
publishes every region and relies on clear labelling (below) so that
nobody mistakes a single household's data for a cohort aggregate.
Raising the threshold is a one-line change to the constant or an
environment variable on the aggregate workflow.

### Per-region views

The per-region outputs are:

- `price_response.json` (import and export price-response scatter, per region)
- `buy_sell_spread.json` (hourly buy and sell prices, per region)
- `curtailment_heatmap.json` (region x hour curtailment heatmap)
- `shadow_prices.json`: `envelope_shadow_heatmap` and
  `grid_envelope_shadow_heatmap` (region x hour shadow-price heatmaps)

For each of these the aggregator counts distinct households per region.
A region with fewer than `K_MIN_REGION` households is withheld from its
own series. Its rows are pooled into a single `NEM` series ("NEM
(pooled regions)" on the dashboard) if the pooled households reach
`K_MIN_REGION`, and are dropped from that view otherwise. Each payload
carries:

- `households`: distinct households behind each published region series
- `suppressed_regions`: regions withheld from their own series
- `rolled_up_into`: `"NEM"` when suppressed rows were pooled, else `null`
- `k_min_region`, `k_advisory` and `suppression_note`

The dashboard labels any region series with fewer than `K_ADVISORY`
households, for example "1 household — individual data", and shows
"suppressed: fewer than k households" in place of the chart for a
suppressed region. `status.json` carries the thresholds and the
per-region household counts under `k_anonymity`.

### Cohort-wide views

The cohort flex stack, counterfactual ledger, assets and V2G views,
the shadow-price-by-hour chart and the headline totals aggregate across
all households and are not split by region. They are **not** subject to
`K_MIN_REGION`. With two households, a cohort-wide series is a sum or
mean over two homes, so it is close to individual data. The dashboard
header notes when the cohort is smaller than `K_ADVISORY`.

Once regions are suppressed without being pooled, a cohort-wide total
minus the published regions can reveal the suppressed households'
contribution. The aggregator does not defend against that differencing.

### Cohort parquet (`data/cohort/`)

- `household_id` is **never** written to the published parquet.
  Per-household grouping (resampling, the asset snapshot and the cohort
  size) happens in memory only.
- Rows in regions below `K_MIN_REGION` are pooled into `NEM` or dropped,
  using the same rule as the per-region views.
- `postcode_prefix` is blanked (null) for any prefix shared by fewer
  than `K_MIN_PREFIX` households. With the current cohort, every prefix
  is blanked.
- Each run rebuilds the parquet tree from scratch, so a suppressed or
  withdrawn date leaves no stale file behind.

The 5-minute and hourly parquet rows are still one household per row
(without the identifier). With one household per region, region and
timestamp are enough to separate households. Removing `household_id`
stops casual linking to the raw tree. It does not make the parquet
anonymous.

### What is not enforced

- **The raw JSONL is public.** `data/raw/<household-id>/` is committed
  to this public repository and licensed CC-BY-4.0 along with the rest
  of `data/`. It carries the household identifier, postcode prefix and
  full 5-minute telemetry for every household. None of the guardrails
  above apply to it. This stays true until the v0.5 relay architecture
  lands and the raw tier moves behind a researcher-access agreement.
- No other dashboard view is grouped by `postcode_prefix`. Since v0.6.0
  the dashboard aggregates by region only.
- Timestamps are the 5-minute interval starts the integration
  publishes. The aggregator does not coarsen them further.

A change that weakens any of these guardrails, including lowering a
threshold once it has been raised, should call that out for privacy
review in the pull request description.

## Data access tiers

Three tiers are defined:

1. **Public (everyone)**. Dashboard views and the cohort parquet at
   `data/cohort/`, subject to the k-anonymity guardrails above.
2. **Researcher (data-use agreement)**. Per-household raw JSONL at
   `data/raw/<household-id>/`. **Currently public** in this repository
   under CC-BY-4.0. It will move behind a researcher-access agreement
   when the v0.5 relay architecture lands, if the cohort grows beyond
   an opt-in friend group. Until then, treat this tier as public.
3. **Contributing household (themselves only)**. Their own data via
   their local Home Assistant, with no privacy considerations beyond
   their own choices.

## Withdrawal and data removal

A contributing household may withdraw at any time:

1. Disable or uninstall the integration in Home Assistant. No further
   snapshots will be published.
2. Open an issue on the repository titled `Withdrawal request:
   <anonymous-household-id>`. Provide the anonymous household
   identifier and a brief statement.
3. The repository maintainer will scrub the historical raw records
   for that identifier from the default branch within 30 days, and
   regenerate the affected cohort aggregates.

Removal is best-effort; git history can be force-pushed but
downstream forks and archive mirrors may retain copies. Withdrawal is
most effective if exercised early.

## Reporting privacy issues

If you believe you have identified a privacy issue with this project,
please report it via the process described in
[`docs/SECURITY.md`](SECURITY.md). Privacy issues are treated with
the same urgency as security issues.

## Changelog

- **2026-10-09**: k-anonymity guardrails implemented in the aggregator
  (#22). The earlier text described a k ≥ 5 rule and a "no individual
  household curves" guarantee that the pipeline did not implement. This
  version documents the configurable thresholds, the current default
  of `K_MIN_REGION = 1` and why, the removal of `household_id` from the
  cohort parquet, and the fact that raw JSONL is public under CC-BY-4.0
  until the relay architecture lands.
- **2026-05-05**: Initial PRIVACY.md, written alongside the v0.4 prep
  changes that move household_id to a UUID v4 default with relaxed
  validation. Documents the Option 1 attribution leak explicitly,
  describes the planned relay architecture for v0.5, and codifies
  the k-anonymity guardrails that already exist in the aggregation
  pipeline.
