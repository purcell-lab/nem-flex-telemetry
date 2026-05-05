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

### Shadow price sensors are null in my records

Shadow prices require HAEO v0.3 or later and are optional. Null is valid. If your HAEO instance is older, upgrade HAEO and the sensors will be picked up automatically on the next coordinator startup (no reconfiguration needed, thanks to the global sweep).

### No data appearing in the repo after an hour

- Check `sensor.<household_id>_push_errors`. If above 0, check the HA logs for `custom_components.nem_flex_telemetry` entries.
- Ensure your HA instance has outbound internet access to `api.github.com`.
- Try the manual push service (Step 8) and watch the logs.

### Entity state is "unavailable" or "unknown"

- Ensure HAEO is running and producing states for the entities you mapped.
- Unavailable numeric entities default to 0.0. Unavailable shadow price entities become null.

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
