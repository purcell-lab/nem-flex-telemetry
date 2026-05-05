# Installation guide: NEM Flex Telemetry

This guide walks through adding your household to the NEM Flex Telemetry community dataset.

**Prerequisites:**
- Home Assistant (2024.1.0 or later) running and accessible.
- [HAEO](https://github.com/hass-energy/haeo) installed and producing sensor entities.
- [HACS](https://hacs.xyz/) installed in your Home Assistant instance.
- A GitHub account.

---

## Step 1: Create a fine-grained GitHub PAT

The integration pushes data on your behalf using a personal access token (PAT) that you control. It is scoped to this single repo with the minimum permission required.

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens) and select **Fine-grained tokens**.
2. Click **Generate new token**.
3. Name it something like `nem-flex-telemetry-<your-ha-hostname>`.
4. Set **Expiration** to at least 1 year (or no expiration).
5. Under **Repository access**, select **Only select repositories**, then choose `purcell-lab/nem-flex-telemetry`.
6. Under **Permissions > Repository permissions**, set **Contents** to **Read and write**. All other permissions should remain set to None.
7. Click **Generate token** and copy it immediately. You will not see it again.

Keep this token secure. If it is leaked, revoke it immediately and create a new one.

---

## Step 2: Add the HACS custom repository

1. In Home Assistant, go to **HACS** (in the sidebar).
2. Click **Integrations**.
3. Click the three-dot menu (top right) and select **Custom repositories**.
4. In the **Repository** field, paste: `https://github.com/purcell-lab/nem-flex-telemetry`
5. Set **Category** to **Integration**.
6. Click **Add**.
7. Close the custom repositories dialog.

---

## Step 3: Install the integration

1. In HACS Integrations, search for **NEM Flex Telemetry**.
2. Click on it, then click **Download** (bottom right).
3. Select version `0.1.0` (or the latest available).
4. Click **Download** to confirm.
5. **Restart Home Assistant.** (Developer Tools > Restart, or Settings > System > Restart.)

---

## Step 4: Config flow walkthrough

After restarting, add the integration:

1. Go to **Settings > Devices and Services > Add Integration**.
2. Search for **NEM Flex Telemetry** and click it.

### Step 4a: GitHub credentials and household identity

- **GitHub Fine-Grained PAT:** paste the token from Step 1.
- **Household ID slug:** a unique lowercase identifier for your household, e.g. `sunshine-coast-01`. Use letters, digits, and hyphens. This becomes your directory name in the repo: `data/raw/<household-id>/`.
- **Postcode prefix:** the first 3 digits of your postcode. For example, if your postcode is 4556, enter `455`. This is the maximum geographic resolution stored.
- **NEM region:** select your NEM dispatch region. Most Queensland households are `QLD1`. If unsure, check [AEMO's map](https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/network-data/network-outage-forecasting).

Click **Submit**. The integration will verify your PAT against the GitHub repo. If it fails, check that your token has `Contents: Read and write` on the correct repo.

### Step 4b: HAEO entity mappings

Map each telemetry field to a sensor entity from your HAEO installation. Default entity IDs are pre-filled based on standard HAEO naming conventions. If your HAEO entities have different IDs, update them here.

All entity IDs must start with `sensor.`.

If you are unsure of your entity IDs, go to **Developer Tools > States** and search for `haeo` to find them.

<!-- Screenshot placeholder: config_flow_entities.png -->

### Step 4c: Privacy and licence agreement

- **Opt in to cohort aggregation** (recommended): your data will be included in the cohort-level parquet files and dashboard views. Uncheck to push raw data to the repo but exclude from cohort aggregation.
- **I agree to publish my telemetry data under CC-BY-4.0** (required): you must agree to this to proceed. Your data will be publicly readable under the Creative Commons Attribution 4.0 International licence.

Click **Submit** to complete setup.

---

## Step 5: Verify data is flowing

Wait up to 1 hour for the first automatic push, or use the manual push service immediately:

1. Go to **Developer Tools > Services**.
2. Search for `nem_flex_telemetry.manual_push`.
3. Call the service.
4. Check [github.com/purcell-lab/nem-flex-telemetry/tree/main/data/raw/](https://github.com/purcell-lab/nem-flex-telemetry/tree/main/data/raw/) for a new directory with your household ID.

You can also check the status sensors created by the integration:
- `sensor.<household_id>_last_push_time`: timestamp of the last successful push.
- `sensor.<household_id>_records_pushed_today`: count pushed since UTC midnight.
- `sensor.<household_id>_push_errors`: cumulative error count (should be 0).

---

## Troubleshooting

### "GitHub auth failed" in config flow

- Verify the PAT has `Contents: Read and write` permission.
- Verify the PAT is scoped to `purcell-lab/nem-flex-telemetry` specifically.
- Check the PAT has not expired.

### No data appearing in the repo after an hour

- Check `sensor.<household_id>_push_errors`. If it is above 0, check the Home Assistant logs for `custom_components.nem_flex_telemetry` entries.
- Ensure your HA instance has outbound internet access to `api.github.com`.
- Try the manual push service (Step 5) and watch the logs.

### Entity state is "unavailable" or "unknown"

- Ensure HAEO is running and producing states for the entities you mapped.
- Unavailable entities default to 0.0. This is intentional to ensure records are never dropped. If your battery is unavailable, `storage_soc_pct` will report 0.

### Integration does not appear in HACS

- Confirm HACS is on version 2.0 or later.
- Confirm you added the repo as category **Integration** (not Plugin or Theme).
- Try clearing the HACS cache: HACS > three-dot menu > Redownload.

### I want to withdraw my data

Raise a [GitHub Issue](https://github.com/purcell-lab/nem-flex-telemetry/issues) titled `[Data removal] <household-id>`. Your raw data directory and any cohort files containing your records will be removed within 5 business days.

---

## Next steps

- Watch the [dashboard](https://purcell-lab.github.io/nem-flex-telemetry/) for your household's data to appear in the cohort views.
- Read [SCHEMA.md](../SCHEMA.md) for the full field specification.
- Read [CONTRIBUTING.md](../CONTRIBUTING.md) to propose schema improvements or new dashboard views.
