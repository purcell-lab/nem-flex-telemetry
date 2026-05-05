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
- For the **public dashboard**, k-anonymity guardrails keep small
  cohorts coarse-grained. We do not render any chart or table that
  could expose an individual household's load curve.
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
   battery installations. Mitigation: pseudonymous identifiers,
   k-anonymity guardrails on aggregated views, no exact location.
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

## k-anonymity guardrails for the public dashboard

The public dashboard at https://purcell-lab.github.io/nem-flex-telemetry/
applies the following rules before rendering any chart:

- **Per-postcode-prefix views require k ≥ 5 households** in that
  prefix. Below this threshold, the prefix is suppressed from any
  geographic chart and rolled up into the parent NEM region.
- **No individual household curves** are rendered on the public
  dashboard, regardless of how many households are in the cohort.
  Single-household exploration is available only to the contributing
  household via their local Home Assistant.
- **Time-series aggregations** are computed at the regional or
  cohort level (mean, p10, p50, p90, p95) rather than per-household.
- **Asset-level breakdowns** (battery vs EV vs solar contribution)
  are aggregated by asset kind across the cohort, not per-household.
- **Postcode-prefix coarsening rule**: if any prefix has fewer than 5
  contributing households, the corresponding rows are dropped from
  the prefix-level cohort tables before publication. Only the
  coarsened tables are committed to the repo and rendered on the
  dashboard. The raw per-household JSONL records remain in
  `data/raw/<household-id>/` for researchers operating under data-use
  agreements, but are never surfaced on the public dashboard.
- **Time alignment**: all timestamps are rounded to the 5-minute
  interval boundary so that no behavioural inference can be drawn
  from sub-interval timing precision.

These guardrails are encoded in the aggregation pipeline
(`scripts/aggregate.py` and `.github/workflows/aggregate.yml`) and
are validated by CI on every pull request. A change that would
weaken any of these guardrails requires an explicit privacy review
in the pull request description.

## Data access tiers

Three tiers are defined:

1. **Public (everyone)**. Aggregated cohort views on the dashboard
   subject to the k-anonymity guardrails above. Aggregated parquet
   files at `data/cohort/`.
2. **Researcher (data-use agreement)**. Per-household raw JSONL at
   `data/raw/<household-id>/`. Currently public during the development
   phase; will move behind a researcher-access agreement at v0.5 if
   the cohort grows beyond an opt-in friend group.
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

- **2026-05-05**: Initial PRIVACY.md, written alongside the v0.4 prep
  changes that move household_id to a UUID v4 default with relaxed
  validation. Documents the Option 1 attribution leak explicitly,
  describes the planned relay architecture for v0.5, and codifies
  the k-anonymity guardrails that already exist in the aggregation
  pipeline.
