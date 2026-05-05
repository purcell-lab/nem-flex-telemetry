# Security model and threat model

This document describes how NEM Flex Telemetry protects the integrity of cohort data and the privacy of participating households. It is intended to be honest about what is and is not protected.

---

## 1. Authentication

**Mechanism:** GitHub OAuth Device Flow ([reference](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow)).

The integration authenticates via GitHub's Device Flow. The user visits https://github.com/login/device on any browser, enters a short code displayed in the HA config flow, and approves access. No passwords or tokens are ever copied by the user.

**Scope:** `public_repo` only ([scope reference](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)). This is the narrowest scope that permits writing to a public repository. It does not grant access to private repositories, user profile data, or any other GitHub resource.

**Token storage:** The OAuth token is stored in the HA config entry data dict, which HA encrypts at rest using the instance's secret key. The token is never written to HA logs. All logging in `device_flow.py` and `github_client.py` explicitly avoids logging the token value at any level.

**Revocation:** Tokens can be revoked at any time from https://github.com/settings/applications without removing the HA integration. After revocation, the coordinator will receive a 401 on the next push attempt and trigger HA's re-authentication flow automatically.

---

## 2. Authorisation

**Path scoping:** Each household may only write to `data/raw/<their-github-login>/**`. This is enforced by the `validate.yml` CI workflow, not by GitHub branch protection (which does not support per-path restrictions for contributors on public repos under free accounts).

The `validate.yml` Action enforces:

- Every commit to `data/raw/**` must come from a verified GitHub user (no anonymous pushes).
- The committed paths must be exactly `data/raw/<commit-author-login>/**` and nowhere else.
- Every record must pass schema v1.1 validation (JSON Schema + jsonschema library).
- Records must not be backdated more than 7 days from the current UTC date.
- No more than 12 records per 5-minute interval per household per day (rate limit protecting against flooding).
- Any commit that fails these checks is reverted by a follow-up bot commit. A comment is posted to the pull request explaining the failure.

**CODEOWNERS:** The `.github/CODEOWNERS` file requires maintainer approval for changes to `/schema/`, `/scripts/`, `/site/`, `/.github/`, and `/custom_components/`. Household data directories are not covered by CODEOWNERS because doing so would block legitimate data pushes.

---

## 3. Threat model

**Honest household with a bug.** If a household's HA instance produces malformed records (e.g. a sensor returning `unavailable`), schema validation catches the error and the CI workflow rejects the commit. The record is not included in cohort aggregation. The household sees a push error in their HA sensor and can diagnose via the HA logs.

**Compromised household token.** If a household's OAuth token is stolen, the attacker can write records to `data/raw/<household-login>/**` only. The CI path-scope guard prevents writing to any other household's directory. The blast radius is one household's folder. The legitimate owner revokes the token at https://github.com/settings/applications. The HA integration automatically prompts re-authentication on the next push attempt.

**Hostile household attempting to write to other folders.** The CI path-scope guard rejects any commit where changed paths include paths outside `data/raw/<authenticated-login>/**`. The commit is reverted, the PR is commented on, and the account becomes eligible for a ban from the cohort.

**Hostile household injecting bad data into their own folder.** A malicious actor may submit plausible-looking but fabricated records into their own folder. The schema check confirms field types and ranges but cannot detect fabrication. The blast radius is one household. Cohort-level statistical outlier detection (planned for v0.3) will flag households whose data is consistently anomalous compared to the regional cohort distribution and queue them for maintainer review.

**Token leak via HA logs.** The integration never logs the OAuth token at any level. The `device_flow.py` module uses `_LOGGER.info` only for non-sensitive state transitions (user_code, verification_uri) and `_LOGGER.debug` for poll loop progress. The `github_client.py` module does not log the Authorization header value. Code review guidance covers this explicitly.

**Data privacy exposure.** Only `postcode_prefix` (3 digits), `region`, and the 13 telemetry fields are published. No NMI, no meter ID, no exact address, no GPS coordinates, no appliance-level data. CC-BY-4.0 terms require attribution but do not restrict re-use, so contributors should be aware their data is publicly readable. The 3-digit postcode prefix covers a geographic area large enough to prevent re-identification in all Australian metro contexts and most regional ones.

---

## 4. What is NOT protected

Being explicit about current limitations:

- **AEMO-grade audit trail.** Signed commits are not yet enforced. A sufficiently motivated attacker who controls a GitHub account could rewrite their own history. Signed commits (GPG) are planned for v0.3 and will be required for institutional cohort partners.

- **Real-time spoofing detection.** The statistical methods described above are retroactive, not real-time. Fabricated data from a new household may appear in cohort aggregations for up to one aggregation cycle before detection flags it.

- **Sybil attacks.** An operator controlling many fake GitHub accounts could create multiple fake households. Mitigation relies on GitHub account age and public contribution history checks at cohort onboarding, which are manual in v0.2. Automated Sybil detection is on the v0.3 roadmap.

- **Schema completeness.** The CI workflow validates field types and ranges. It does not validate physical plausibility (e.g. a battery SOC of 99% followed immediately by 1% without a corresponding discharge event). Plausibility checks are on the v0.3 roadmap.

---

## 5. Reporting vulnerabilities

Email **mark@purcell.id.au** with the subject line `NEM Flex Security`.

Please include a description of the issue, the affected component (integration, CI workflow, schema, dashboard), and any proof-of-concept you have developed.

**Public disclosure timeline:** Maintainer will acknowledge within 5 business days. A fix will be developed and merged within 30 days of acknowledgement. Public disclosure of the vulnerability and fix will occur within 7 days of the fix being deployed. If the 30-day window cannot be met, the reporter will be notified with a revised timeline.

---

## 6. Future hardening roadmap (v0.3+)

- **GPG-signed commits required.** All household commits to `data/raw/**` will be required to carry a valid GPG signature, cryptographically binding each record to a specific GitHub identity. This is the AEMO-grade audit trail the current schema lacks.

- **GitHub App replacing OAuth App.** A GitHub App installation per household provides finer-grained permission control than an OAuth App (App installations are per-repo and per-path, not per-user), and supports installation-level token rotation.

- **OIDC federation for institutional cohort partners.** Energy retailers, aggregators, and research institutions joining the cohort as institutional contributors will authenticate via OpenID Connect federation rather than individual OAuth tokens, enabling automated key rotation and audit-log integration with their own identity providers.
