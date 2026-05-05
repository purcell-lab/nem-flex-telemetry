# Installation guide: NEM Flex Telemetry

This guide walks through adding your household to the NEM Flex Telemetry community dataset.

**Prerequisites:**
- Home Assistant (2024.1.0 or later) running and accessible.
- [HAEO](https://github.com/hass-energy/haeo) installed and producing sensor entities.
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
3. Select version `0.2.0` (or the latest available).
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

No tokens to copy. No settings panels to navigate. The integration handles the rest.

---

## Step 4: Household identity

Once authorised, Home Assistant displays your GitHub login (read-only, filled automatically). Choose:

- **Household ID:** a unique lowercase identifier for your household, e.g. `sunshine-coast-01`. Use letters, digits, and hyphens (3-64 characters). This becomes your directory name in the repo: `data/raw/<household-id>/`.
- **Postcode prefix:** the first 3 digits of your postcode. For example, if your postcode is 4556, enter `455`. This is the maximum geographic resolution stored.
- **NEM region:** select your NEM dispatch region. Most Queensland households are `QLD1`. If unsure, check [AEMO's map](https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/network-data/network-outage-forecasting).

Click **Submit**.

---

## Step 5: HAEO entity mapping (auto-discovery)

The integration scans your Home Assistant instance for known HAEO entity IDs.

**If HAEO is installed with default entity names**, all required entities are detected automatically. You will see a confirmation screen listing the detected entities. Review them and click **Submit** to accept, or tick **Customise mapping** to override any.

**If some entities are missing**, the form pre-fills what was found and asks you to pick the remaining ones from a sensor dropdown. Only the missing fields are shown.

**If HAEO is not detected**, the full manual mapping form opens. Map each of the 13 schema fields to the appropriate sensor entity. All fields use a dropdown picker so you do not need to type entity IDs by hand. See [https://github.com/hass-energy/haeo](https://github.com/hass-energy/haeo) for the reference implementation.

Note on flex headroom: if your HAEO instance does not expose `flex_available_up/down` entities directly, the integration derives them from your battery's maximum charge and discharge rate. A one-time INFO log message at startup explains this.

---

## Step 6: Privacy and licence agreement

- **Opt in to cohort aggregation** (recommended): your data will be included in the cohort-level parquet files and dashboard views. Uncheck to push raw data to the repo but exclude from cohort aggregation.
- **I agree to publish my telemetry data under CC-BY-4.0** (required): you must agree to this to proceed. Your data will be publicly readable under the Creative Commons Attribution 4.0 International licence.

Click **Submit** to complete setup.

---

## Step 7: Verify data is flowing

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

## Revoking access

To revoke the integration's access to your GitHub account at any time:

1. Go to [https://github.com/settings/applications](https://github.com/settings/applications).
2. Find "NEM Flex Telemetry" under **Authorised OAuth Apps**.
3. Click **Revoke**.

After revocation, the integration will detect the invalid token on the next push attempt and prompt you to re-authorise via Device Flow from the Home Assistant notifications panel.

---

## Troubleshooting

### Config flow shows an authorisation error

- Check that you entered the code on **https://github.com/login/device** (not github.com/login).
- Codes expire after 15 minutes. If you see "code expired", click "Try again" in the config flow.
- If GitHub shows "authorisation denied", you may have clicked the wrong button. Click "Try again" to restart.

### No data appearing in the repo after an hour

- Check `sensor.<household_id>_push_errors`. If it is above 0, check the Home Assistant logs for `custom_components.nem_flex_telemetry` entries.
- Ensure your HA instance has outbound internet access to `api.github.com`.
- Try the manual push service (Step 7) and watch the logs.

### Entity state is "unavailable" or "unknown"

- Ensure HAEO is running and producing states for the entities you mapped.
- Unavailable entities default to 0.0 to ensure records are never dropped. If your battery is unavailable, `storage_soc_pct` will report 0.

### "Token invalid" notification appears

Your OAuth token has been revoked or has expired. Click the notification or go to Settings > Devices and Services, find NEM Flex Telemetry, and click **Re-authenticate** to run Device Flow again. No data is lost; buffered records are re-pushed after re-authentication.

### Integration does not appear in HACS

- Confirm HACS is on version 2.0 or later.
- Confirm you added the repo as category **Integration** (not Plugin or Theme).
- Try clearing the HACS cache: HACS > three-dot menu > Redownload.

### I want to withdraw my data

Raise a [GitHub Issue](https://github.com/purcell-lab/nem-flex-telemetry/issues) titled `[Data removal] <household-id>`. Your raw data directory and any cohort files containing your records will be removed within 5 business days.

---

## Next steps

- Watch the [dashboard](https://purcell-lab.github.io/nem-flex-telemetry/) for your household's data to appear in the cohort views.
- Read [SCHEMA.md](../SCHEMA.md) for the full 13-field specification.
- Read [docs/SECURITY.md](SECURITY.md) for the security and threat model.
- Read [CONTRIBUTING.md](../CONTRIBUTING.md) to propose schema improvements or new dashboard views.
