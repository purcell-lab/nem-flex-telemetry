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

**Path scoping:** Each household may only write to `data/raw/<their-github-login>/**`. This is enforced by the `validate.yml` CI workflow.

The `validate.yml` Action enforces:

- Every commit to `data/raw/**` must come from a verified GitHub user.
- The committed paths must be exactly `data/raw/<commit-author-login>/**` and nowhere else.
- Every record must pass schema v2.0 validation (JSON Schema + jsonschema library).
- `schema_version` must equal `'2.0'`. Records with any other value are rejected outright.
- All price fields (`price_signal_seen`, `price_export_seen`, and all shadow price fields) must be within the $/kWh range of -2.0 to 20.0. This range check is an additional anti-injection guard: a compromised instance that somehow emitted $/MWh values (e.g. 8750 instead of 0.0875) would be caught here before entering the cohort. Records outside this range cannot represent plausible $/kWh prices under any NEM scenario.
- Records must not be backdated more than 7 days from the current UTC date.
- Any commit that fails these checks is reverted by a follow-up bot commit.

**CODEOWNERS:** The `.github/CODEOWNERS` file requires maintainer approval for changes to `/schema/`, `/scripts/`, `/site/`, `/.github/`, and `/custom_components/`. Household data directories are not covered by CODEOWNERS because doing so would block legitimate data pushes.

---

## 3. Threat model

**Honest household with a bug.** If a household's HA instance produces malformed records (e.g. a sensor returning `unavailable`), schema validation catches the error and the CI workflow rejects the commit. The household sees a push error in their HA sensor.

**Compromised household token.** If a household's OAuth token is stolen, the attacker can write records to `data/raw/<household-login>/**` only. The CI path-scope guard prevents writing to any other household's directory. The legitimate owner revokes the token at https://github.com/settings/applications.

**Hostile household attempting to write to other folders.** The CI path-scope guard rejects any commit where changed paths include paths outside `data/raw/<authenticated-login>/**`.

**Hostile household injecting bad data into their own folder.** Schema validation confirms field types, $/kWh ranges, and schema\_version. The $/kWh range check (-2.0 to 20.0) is the primary price-injection guard: it prevents any record with implausible prices from entering the cohort. A record claiming a price of 8750 $/kWh would fail with a clear error message explaining that prices must be in $/kWh, not $/MWh.

**Token leak via HA logs.** The integration never logs the OAuth token at any level.

**Data privacy exposure.** Only `postcode_prefix` (3 digits), `region`, and the telemetry fields are published. No NMI, no meter ID, no exact address, no GPS coordinates, no appliance-level data. Shadow prices reveal the marginal value of energy constraints at the household level. They do not identify the household beyond the 3-digit postcode prefix.

---

## 4. What is NOT protected

- **AEMO-grade audit trail.** Signed commits are not yet enforced. GPG-signed commits are planned for v0.3 and will be required for institutional cohort partners.
- **Real-time spoofing detection.** Statistical anomaly detection is retroactive.
- **Sybil attacks.** Multiple fake households controlled by one actor. Mitigation relies on GitHub account age checks, which are manual in v0.3.
- **Schema completeness.** The CI workflow validates field types, ranges, and schema version. It does not validate physical plausibility (e.g. SOC jumping 40% in one 5-minute interval).

---

## 5. Reporting vulnerabilities

Email **mark@purcell.id.au** with the subject line `NEM Flex Security`.

**Public disclosure timeline:** Maintainer will acknowledge within 5 business days. A fix will be developed and merged within 30 days of acknowledgement. Public disclosure occurs within 7 days of the fix being deployed.

---

## 6. Future hardening roadmap (v0.3+)

- **GPG-signed commits required.** All household commits to `data/raw/**` will be required to carry a valid GPG signature.
- **GitHub App replacing OAuth App.** Finer-grained per-repo, per-path permission control.
- **OIDC federation for institutional cohort partners.** Authentication via OpenID Connect rather than individual OAuth tokens.
