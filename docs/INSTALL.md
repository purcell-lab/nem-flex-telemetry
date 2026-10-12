# Installation guide: NEM Flex Telemetry

This guide walks through adding your household to the NEM Flex Telemetry community dataset.

**Prerequisites:**
- Home Assistant (2024.1.0 or later) running and accessible.
- [HAEO](https://github.com/hass-energy/haeo) installed and producing sensor entities, including shadow price sensors (HAEO v0.3+ recommended).
- [HACS](https://hacs.xyz/) installed in your Home Assistant instance.
- A GitHub account.

---

## Step 1: Add the HACS custom repository

1. In Home Assistant, go to **HACS** (in the sidebar).
2. Click **Integrations**.
3. Click the three-dot menu (top right) and select **Custom repositories**.
4. In the **Repository** field, paste: `https://github.com/purcell-lab/nem-flex-telemetry`
5. Set **Category** to **Integration**.
6. Click **Add**.
7. Close the custom repositories dialog.

---

## Step 2: Install the integration

1. In HACS Integrations, search for **NEM Flex Telemetry**.
2. Click on it, then click **Download** (bottom right).
3. Select version `0.3.0` (or the latest available).
4. Click **Download** to confirm.
5. **Restart Home Assistant.** (Developer Tools > Restart, or Settings > System > Restart.)

---

## Step 3: Authorise via GitHub Device Flow

After restarting, add the integration:

1. Go to **Settings > Devices and Services > Add Integration**.
2. Search for **NEM Flex Telemetry** and click it.
3. The landing screen explains Device Flow. Click **Submit** to continue.
4. A code appears on screen, for example: `ABCD-1234`.
5. On any device (phone, laptop, tablet), open **https://github.com/login/device**.
6. Enter the code shown in Home Assistant.
7. GitHub will ask you to approve access for "NEM Flex Telemetry". Click **Authorise**.
8. Return to Home Assistant. The config flow will continue automatically within a few seconds.

No tokens to copy. No settings panels to navigate.

---

## Step 4: Household identity

Once authorised, Home Assistant displays your GitHub login (read-only). Choose:

- **Household ID:** a unique lowercase identifier, e.g. `sunshine-coast-01`. Letters, digits, hyphens (3-64 characters). Becomes your directory name: `data/raw/<household-id>/`.
- **Postcode prefix:** first 3 digits of your postcode. For example, if your postcode is 4556, enter `455`.
- **NEM region:** select your NEM dispatch region. Most Queensland households are `QLD1`.

Click **Submit**.

---

## Step 5: HAEO entity mapping and global sweep

The integration scans your Home Assistant instance for known HAEO entity IDs. In v0.3 this includes:

**Core grid and price entities:**
- `sensor.grid_active_power` (net import/export)
- `number.solar_forecast` (solar generation)
- `sensor.load_power` (total load)
- `number.grid_import_price` (buy price, $/kWh)
- `number.grid_export_price` (sell price, $/kWh)
- `number.grid_import_limit` (import envelope)
- `number.grid_export_limit` (export envelope)

**Shadow price sensors (HAEO v0.3+):**
- `sensor.load_forecast_limit_shadow_price`
- `sensor.solar_forecast_limit_shadow_price`
- `sensor.grid_max_import_power_shadow_price`
- `sensor.grid_max_export_power_shadow_price`

If your HAEO instance exposes these shadow price sensors, they are picked up automatically. If not, those fields will be null in your records (valid: the schema permits null).

A **global sweep** also runs at this step, matching any entity not in the named list against these patterns:
- `sensor.*_shadow_price`
- `sensor.*_state_of_charge`
- `sensor.*_active_power`
- `number.grid_*`
- `binary_sensor.*_(plugged|charging|connected)`

Any matched entities not already mapped are shown as "detected but unmapped" in the config flow. The global sweep runs again on every reload, so newly added HAEO entities are picked up automatically without reconfiguring.

---

### Optional: use Nimbus as the record source

If you run [Nimbus](https://github.com/code-imstillalive/nimbus) v0.94.443 or later, this integration can relay the complete record Nimbus publishes on `sensor.nimbus_flex_telemetry`, instead of building one from HAEO entities. Nimbus supplies the measurements. This integration still handles GitHub sign-in, the household ID, the buffer and the push, so Nimbus never needs a GitHub token.

1. In Nimbus, turn on `switch.nimbus_solver_flex_signals_enabled`. Nimbus only builds a record while flex ranging is on, and ranging makes each solve slower (nimbus#1634).
2. Go to Settings > Devices and Services > NEM Flex Telemetry > Configure.
3. Set **Record source** to **Nimbus** and save. The entity mappings are then ignored.
4. Check the `source` and `source_status` attributes on any NEM Flex Telemetry sensor. A `source_status` of none means records are flowing. Otherwise it says why no record was relayed.

A Nimbus record is relayed unchanged, and only if its `region` and `postcode_prefix` match this integration's settings. Each interval is relayed once, even if the sensor updates twice.

## Step 6: Asset configuration

The config flow shows a summary of the discovered assets (home battery, EV1, EV2) and asks for their energy capacities:

- **Home battery capacity (kWh):** default 13.5 kWh. Adjust to your battery's usable capacity.
- **EV1 capacity (kWh):** default 75.0 kWh. Adjust to your first EV's usable battery size.
- **EV2 capacity (kWh):** default 60.0 kWh. Adjust to your second EV's usable battery size.

These are used to compute per-asset kWh stored in the `assets[]` array. HAEO does not always expose these values directly, so they are asked once during setup.

**Note on connection state:** The integration infers EV plug state from shadow prices and setpoints. It does not require any `binary_sensor` plug entity. See [SCHEMA.md](../SCHEMA.md#connection-state-inference) for the inference rules.

---

## Step 7: Privacy and licence agreement

- **Opt in to cohort aggregation** (recommended): your data will be included in the cohort-level parquet files and dashboard views.
- **I agree to publish my telemetry data under CC-BY-4.0** (required): you must agree to proceed. Your data will be publicly readable.

Click **Submit** to complete setup.

---

## Step 8: Verify data is flowing

Wait up to 1 hour for the first automatic push, or use the manual push service:

1. Go to **Developer Tools > Services**.
2. Search for `nem_flex_telemetry.manual_push`.
3. Call the service.
4. Check [github.com/purcell-lab/nem-flex-telemetry/tree/main/data/raw/](https://github.com/purcell-lab/nem-flex-telemetry/tree/main/data/raw/) for your household directory.

Status sensors:
- `sensor.<household_id>_last_push_time`: timestamp of last successful push.
- `sensor.<household_id>_records_pushed_today`: count pushed since UTC midnight.
- `sensor.<household_id>_push_errors`: cumulative error count (should be 0).

---

## Revoking access

1. Go to [https://github.com/settings/applications](https://github.com/settings/applications).
2. Find "NEM Flex Telemetry" under **Authorised OAuth Apps**.
3. Click **Revoke**.

After revocation, the integration will detect the invalid token on the next push attempt and prompt re-authorisation.

---

## Troubleshooting

### Config flow shows an authorisation error

- Check that you entered the code on **https://github.com/login/device** (not github.com/login).
- Codes expire after 15 minutes. If you see "code expired", click "Try again" in the config flow.

### Push fails with HTTP 404

GitHub returns 404, not 403, when your account can read the repository but cannot write to it. Records are pushed straight to `purcell-lab/nem-flex-telemetry`, so the maintainer must invite your GitHub account first.

1. [Open an issue](https://github.com/purcell-lab/nem-flex-telemetry/issues/new) titled `[Access request] <your GitHub username>`.
2. Accept the invitation from your GitHub notifications or email.
3. Wait for the next hourly push, or run the manual push service (Step 8).

While access is missing, the integration keeps up to 24 hours of records, shows a repair under Settings > System > Repairs, and does not retry the failed push within the same hour. Setup (Step 3) also checks write access and warns you before you continue. The repair clears after the first successful push.

### Shadow price sensors are null in my records

Shadow prices require HAEO v0.3 or later and are optional. Null is valid. If your HAEO instance is older, upgrade HAEO and the sensors will be picked up automatically on the next coordinator startup (no reconfiguration needed, thanks to the global sweep).

### No data appearing in the repo after an hour

- Check `sensor.<household_id>_push_errors`. If above 0, check the HA logs for `custom_components.nem_flex_telemetry` entries.
- Ensure your HA instance has outbound internet access to `api.github.com`.
- Try the manual push service (Step 8) and watch the logs.

### Entity state is "unavailable" or "unknown"

- Ensure HAEO is running and producing states for the entities you mapped.
- If a required input (net import, solar, house load, buy price or sell price) is unavailable, the interval is skipped rather than published as zeros. After the first 15 minutes from startup, one WARNING names the field and entity, and the `skipped_intervals` attribute counts the skipped intervals. Unavailable optional numeric entities default to 0.0, and unavailable shadow price entities become null.
- If a record fails validation, for example a market price outside -2.0 to 30.0 $/kWh, or a shadow price outside the -1000 to 1000 sanity guard, the interval is skipped and counted in the `validation_errors` attribute. That count is kept across restarts. One WARNING names each failing field. Shadow prices are LP duals, not market prices, and can reach several $/kWh when a constraint binds, for example a full battery at the export limit. They are accepted (#30).

### Recovering missed intervals

Intervals can be missing from your day files because Home Assistant restarted, a push kept failing for longer than the 24-hour buffer, an input was briefly unavailable, or a record failed validation. Until #37 raised the market price window to $30/kWh, intervals with a market price above $20/kWh were rejected this way. The integration can rebuild these intervals from the Home Assistant recorder history of the same entities it reads live, as long as the recorder still has that history.

**Deadline:** the recorder keeps history for `purge_keep_days`, which is 10 days by default. An interval older than that cannot be rebuilt. Recover missed intervals within that time.

**Automatic:** the first time Home Assistant starts with a new version of the integration, it checks the last 10 days in the background (or less if your recorder keeps less, and never before your first published record or your consent). It waits for Home Assistant to finish starting and for the required inputs to publish (at most 15 minutes), then pushes any intervals it could rebuild. One INFO log line summarises the result. If GitHub could not be read or the push failed, it tries again at the next start.

**Manually:** call the `nem_flex_telemetry.rebuild_intervals` service from **Developer Tools > Actions**:

```yaml
action: nem_flex_telemetry.rebuild_intervals
data:
  start: "2026-10-10 07:00:00"   # optional, Home Assistant time zone
  end: "2026-10-10 08:00:00"     # optional
  dry_run: true                  # report only, push nothing
```

Without `start` and `end` it checks the same window as the automatic run. Tick **Return response** to see the summary: intervals checked, already published, rebuilt (with their times), pushed, and skipped with a reason for each, such as no recorder history or a required input unavailable at the time. Run it with `dry_run: true` first if you want to see what would be pushed.

How rebuilt intervals compare with live ones:

- Each entity is read as it was at the same point in the interval at which the live integration reads it, and the record goes through the same required-input check and validation. Intervals already in your day files or still in the local buffer are never rebuilt, and the push skips any interval that reached GitHub in the meantime.
- EV connection state and the charger allocation depend on earlier intervals. They are replayed from the recorder starting one hour before each gap, so they can differ from what the live integration would have published if it had been running without interruption for longer than that.
- Entities excluded from the recorder (`recorder: exclude:` in `configuration.yaml`) have no history. If a required input is excluded, those intervals are skipped. Excluded optional entities are treated as unavailable, as they would be live.
- With the Nimbus source, the record is taken from the history of `sensor.nimbus_flex_telemetry` (its `record` attribute). The recorder drops attributes larger than 16 KB, and Nimbus only publishes while flex ranging is on, so some intervals may have no record to recover.

### "Token invalid" notification appears

Your OAuth token has been revoked or expired. Click the notification or go to Settings > Devices and Services, find NEM Flex Telemetry, and click **Re-authenticate** to run Device Flow again.

### I want to withdraw my data

Raise a [GitHub Issue](https://github.com/purcell-lab/nem-flex-telemetry/issues) titled `[Data removal] <household-id>`. Your raw data directory and any cohort files containing your records will be removed within 5 business days.

---

## Next steps

- Watch the [dashboard](https://purcell-lab.github.io/nem-flex-telemetry/) for your data to appear.
- Read [SCHEMA.md](../SCHEMA.md) for the full v2.0 specification.
- Read [docs/SECURITY.md](SECURITY.md) for the security and threat model.
- Read [CONTRIBUTING.md](../CONTRIBUTING.md) to propose schema improvements.
