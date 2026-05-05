# Contributing to NEM Flex Telemetry

Thank you for contributing. This document covers four paths: adding a household, proposing schema changes, contributing dashboard views, and general code contributions.

---

## How to add a household

The primary contribution is participating as a data-contributing household. Follow [docs/INSTALL.md](docs/INSTALL.md) for the full guide.

In short:
1. Add this repo as a HACS custom integration source.
2. Install and configure the "NEM Flex Telemetry" integration.
3. Authorise via GitHub Device Flow when prompted. No tokens to manage.
4. Confirm your HAEO entity mapping (usually auto-detected).
5. Opt in to CC-BY-4.0 data licence and cohort participation.
6. Verify data flows to `data/raw/<your-household-id>/`.

If you hit any issues, open a GitHub Issue tagged `[install]`.

---

## How to propose schema changes

The schema is now at **v1.1** (13 fields, added `price_export_seen`). It is defined in [SCHEMA.md](SCHEMA.md) and [schema/telemetry.schema.json](schema/telemetry.schema.json). Changes to it must be backward-compatible or versioned.

**RFC process:**

1. Open a GitHub Issue titled `[RFC] <proposed change>`. Describe:
   - What field(s) are affected.
   - Why the change is needed.
   - What existing data migration (if any) is required.
   - Whether the change is backward-compatible (additive) or breaking.
2. Allow at least 2 weeks for community discussion.
3. For breaking changes (removing or renaming fields, changing units), a new `SCHEMA_VERSION` must be introduced, and the aggregation script must handle both versions.
4. After consensus, open a PR with:
   - Updated SCHEMA.md
   - Updated schema/telemetry.schema.json (bump `"version"`)
   - Updated coordinator.py validation schema
   - Updated scripts/aggregate.py if the change affects computed views
   - A migration note in SCHEMA.md under a new `### Migration` heading
5. Two approvals required before merge.

**Entity mapping contributions:** If your HAEO instance uses entity names not listed as defaults in `const.py`'s `DEFAULT_HAEO_ENTITIES`, please open a PR adding them to the fallback lists with a note of which integration or HAEO version they come from. Contributions for EMHASS, Sigenergy, SolarEdge, and other optimiser integrations are welcome.

---

## How to contribute dashboard views

The dashboard at [site/index.html](site/index.html) uses Observable Plot loaded from CDN. No build step is needed.

To add or improve a chart:
1. Fork the repo and create a branch named `dashboard/<your-view-name>`.
2. Edit `site/index.html` and/or `scripts/aggregate.py` (for new derived JSON outputs).
3. Test locally: open `site/index.html` in a browser pointed at a local server that serves `site/data/sample.json`.
   ```bash
   cd site && python -m http.server 8080
   ```
4. Open a PR with a screenshot of the new view.

Dashboard design constraints:
- Use the colour palette: INK `#0E1416`, TEAL `#4FB3BF`, RUST `#D78562`, GOLD `#E8B254`, PAPER `#F2EFE7`, RED `#E05252` (negative FiT highlights only).
- No build step, no node_modules, no bundlers. Observable Plot and Tailwind from CDN only.
- All new views must have a corresponding derived JSON file in `site/data/` produced by `scripts/aggregate.py`.

---

## Code contributions

General code contributions (integration bug fixes, aggregation improvements, new sensors):

1. Fork and create a branch named `fix/<description>` or `feat/<description>`.
2. Write type-annotated Python. Use `async` where applicable.
3. Follow existing patterns (ConfigEntry, DataUpdateCoordinator, async aiohttp for GitHub I/O).
4. Add or update comments explaining non-obvious logic.
5. Test against a local Home Assistant instance if possible.
6. Open a PR. One approval required for minor fixes, two for substantive changes.

No formal test suite yet (v0.2). PRs adding pytest coverage are very welcome.

---

## Code of conduct

This project follows the [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Be direct and constructive. Energy policy debates are welcome in Issues. Personal attacks are not.

---

## DCO sign-off

All commits must be signed off with a Developer Certificate of Origin:

```
git commit -s -m "your message"
```

This adds a `Signed-off-by: Your Name <your@email.com>` line to the commit, indicating you certify the DCO v1.1 (https://developercertificate.org/).

---

## Licensing

By contributing code, you agree your contribution is licensed under MIT.
By contributing data (as a participating household), you agree your data is published under CC-BY-4.0.
