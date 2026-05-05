"""Config flow for NEM Flex Telemetry integration.

Flow steps (initial setup):
  1. async_step_user              -- landing page, explains Device Flow
  2. async_step_device_auth       -- requests device code from GitHub
  3. async_step_show_code         -- shows user_code, polls for token in background
  4. async_step_identity          -- household ID, postcode prefix, NEM region
  5. async_step_entities_confirm  -- all HAEO entities auto-detected (confirm or customise)
     async_step_entities_partial  -- some entities missing (pre-filled + missing fields)
     async_step_entities_manual   -- no HAEO detected (full manual form)
  6. async_step_assets            -- discovered assets summary, EV/battery capacity config,
                                     unmapped entity report
  7. async_step_consent           -- CC-BY-4.0 licence + cohort participation
  8. async_step_auth_error        -- Device Flow failure with retry/abort options

Re-authentication flow (triggered when the coordinator detects a 401):
  R1. async_step_reauth           -- entry point registered by HA
  R2. async_step_reauth_confirm   -- skip straight to device_auth -> show_code -> done
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ASSET_DEFAULTS,
    CONF_CONSENT_TIMESTAMP,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_SHADOW_ENVELOPE_EXPORT,
    CONF_ENTITY_SHADOW_ENVELOPE_IMPORT,
    CONF_ENTITY_SHADOW_LOAD_FORECAST,
    CONF_ENTITY_SHADOW_SOLAR_FORECAST,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_EV1_CAPACITY_KWH,
    CONF_EV2_CAPACITY_KWH,
    CONF_GITHUB_LOGIN,
    CONF_HOME_BATTERY_CAPACITY_KWH,
    CONF_HOUSEHOLD_ID,
    CONF_LICENCE_AGREED,
    CONF_OPT_IN_COHORT,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_ENTITY_MAPPINGS,
    DOMAIN,
    NEM_REGIONS,
)
from .device_flow import (
    DeviceFlowDenied,
    DeviceFlowError,
    DeviceFlowExpired,
    DeviceFlowInvalid,
    DeviceFlowNetworkError,
    DeviceFlowSession,
    fetch_authenticated_user,
)
from .discovery import (
    build_entity_map,
    classify_discovery_result,
    discover_haeo_entities,
    run_global_sweep,
)

_LOGGER = logging.getLogger(__name__)

# Validation patterns
HOUSEHOLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")
POSTCODE_PREFIX_RE = re.compile(r"^[0-9]{3}$")

# EntitySelector for sensor domain (gives users a dropdown picker)
_ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=False)
)


def _validate_household_id(value: str) -> str:
    """Validate household ID is a lowercase slug."""
    if not HOUSEHOLD_ID_RE.match(value):
        raise vol.Invalid(
            "Household ID must be a lowercase slug (letters, digits, hyphens), "
            "3-64 characters, not starting with a hyphen. Example: sunshine-coast-01"
        )
    return value


def _validate_postcode_prefix(value: str) -> str:
    """Validate postcode prefix is exactly 3 digits."""
    if not POSTCODE_PREFIX_RE.match(value):
        raise vol.Invalid(
            "Postcode prefix must be exactly 3 digits (e.g. 456 for the 456x postcode zone)."
        )
    return value


def _entity_schema(fields: list[str], defaults: dict[str, str]) -> vol.Schema:
    """Build a voluptuous Schema for a subset of entity fields using EntitySelector."""
    return vol.Schema(
        {
            vol.Required(field, default=defaults.get(field, "")): _ENTITY_SELECTOR
            for field in fields
        }
    )


class NemFlexTelemetryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the NEM Flex Telemetry config flow.

    Guides the user through GitHub Device Flow authorisation, household
    identity capture, HAEO entity auto-discovery (with manual fallback),
    asset configuration, and consent.
    """

    VERSION = 2

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._data: dict[str, Any] = {}
        self._device_flow: dict[str, Any] = {}
        self._auth_error: str = ""
        self._discovery_best: dict[str, str | None] = {}
        self._discovery_candidates: dict[str, list[str]] = {}
        self._unmapped_entities: list[str] = []
        self._is_reauth: bool = False
        self._reauth_entry_id: str | None = None

    # -----------------------------------------------------------------------
    # Step 1: Landing page
    # -----------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Landing step. Explains OAuth Device Flow; user clicks Continue."""
        if user_input is not None:
            return await self.async_step_device_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={},
        )

    # -----------------------------------------------------------------------
    # Step 2: Request device code
    # -----------------------------------------------------------------------

    async def async_step_device_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Request a device code from GitHub and advance to the code display step."""
        session = DeviceFlowSession()
        try:
            code_data = await session.request_device_code()
        except DeviceFlowNetworkError as exc:
            _LOGGER.error("Device Flow: network error requesting device code: %s", exc)
            self._auth_error = str(exc)
            return await self.async_step_auth_error()

        self._device_flow = {
            "device_code": code_data["device_code"],
            "user_code": code_data["user_code"],
            "verification_uri": code_data.get(
                "verification_uri", "https://github.com/login/device"
            ),
            "verification_uri_complete": code_data.get("verification_uri_complete", ""),
            "interval": code_data.get("interval", 5),
            "expires_in": code_data.get("expires_in", 900),
        }
        return await self.async_step_show_code()

    # -----------------------------------------------------------------------
    # Step 3: Show user code, poll for token in background
    # -----------------------------------------------------------------------

    async def async_step_show_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the user code and poll for the access token in the background."""
        if not hasattr(self, "_poll_task_started"):
            self._poll_task_started = True
            self.hass.async_create_task(self._poll_for_token())

        return self.async_show_progress(
            step_id="show_code",
            progress_action="waiting_for_user",
            description_placeholders={
                "user_code": self._device_flow.get("user_code", ""),
                "verification_uri": "https://github.com/login/device",
                "verification_uri_complete": self._device_flow.get(
                    "verification_uri_complete", ""
                ),
            },
        )

    async def _poll_for_token(self) -> None:
        """Background task: poll GitHub for the access token."""
        session = DeviceFlowSession()
        df = self._device_flow
        try:
            token = await session.poll_for_token(
                device_code=df["device_code"],
                interval=df["interval"],
                expires_in=df["expires_in"],
            )
            user_info = await fetch_authenticated_user(token)
            self._data[CONF_TOKEN] = token
            self._data[CONF_GITHUB_LOGIN] = user_info.get("login", "")
            self.async_show_progress_done(next_step_id="identity")
        except DeviceFlowExpired:
            self._auth_error = "device_flow_expired"
            self.async_show_progress_done(next_step_id="auth_error")
        except DeviceFlowDenied:
            self._auth_error = "device_flow_denied"
            self.async_show_progress_done(next_step_id="auth_error")
        except DeviceFlowInvalid:
            self._auth_error = "device_flow_invalid"
            self.async_show_progress_done(next_step_id="auth_error")
        except DeviceFlowNetworkError as exc:
            self._auth_error = str(exc)
            self.async_show_progress_done(next_step_id="auth_error")

    # -----------------------------------------------------------------------
    # Step 4: Household identity
    # -----------------------------------------------------------------------

    async def async_step_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect household ID, postcode prefix, and NEM region."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                _validate_household_id(user_input[CONF_HOUSEHOLD_ID])
            except vol.Invalid:
                errors[CONF_HOUSEHOLD_ID] = "invalid_household_id"

            try:
                _validate_postcode_prefix(user_input[CONF_POSTCODE_PREFIX])
            except vol.Invalid:
                errors[CONF_POSTCODE_PREFIX] = "invalid_postcode_prefix"

            if not errors:
                self._data.update(user_input)
                return await self._async_step_entities_start()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOUSEHOLD_ID): str,
                vol.Required(CONF_POSTCODE_PREFIX): str,
                vol.Required(CONF_REGION): vol.In(NEM_REGIONS),
            }
        )
        return self.async_show_form(
            step_id="identity",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "github_login": self._data.get(CONF_GITHUB_LOGIN, ""),
            },
        )

    # -----------------------------------------------------------------------
    # Entity discovery routing
    # -----------------------------------------------------------------------

    async def _async_step_entities_start(self) -> FlowResult:
        """Run discovery, global sweep, and route to the appropriate entity step."""
        self._discovery_best, self._discovery_candidates = (
            await discover_haeo_entities(self.hass)
        )
        # Build the set of already-mapped entities for the sweep
        already_mapped = set(
            v for v in self._discovery_best.values() if v is not None
        )
        self._unmapped_entities = run_global_sweep(
            self.hass, already_mapped=already_mapped
        )

        mode, missing = classify_discovery_result(self._discovery_best)

        if mode == "all":
            return await self.async_step_entities_confirm()
        if mode == "partial":
            return await self.async_step_entities_partial()
        return await self.async_step_entities_manual()

    # -----------------------------------------------------------------------
    # Step 5a: All entities found, show confirmation
    # -----------------------------------------------------------------------

    async def async_step_entities_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """All HAEO entities detected. Show summary and offer confirm or customise."""
        if user_input is not None:
            if user_input.get("customise"):
                return await self.async_step_entities_manual()
            self._data.update(self._discovery_best)  # type: ignore[arg-type]
            return await self.async_step_assets()

        summary_lines = [
            f"{k}: {v}" for k, v in self._discovery_best.items() if v is not None
        ]
        return self.async_show_form(
            step_id="entities_confirm",
            data_schema=vol.Schema(
                {vol.Optional("customise", default=False): bool}
            ),
            description_placeholders={
                "detected_entities": "\n".join(summary_lines),
            },
        )

    # -----------------------------------------------------------------------
    # Step 5b: Some entities missing, show partial form
    # -----------------------------------------------------------------------

    async def async_step_entities_partial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Some entities missing. Pre-fill what was found; ask for the rest."""
        _, missing = classify_discovery_result(self._discovery_best)
        errors: dict[str, str] = {}

        if user_input is not None:
            merged = build_entity_map(self._discovery_best, user_input)
            for field in missing:
                if field not in merged or not merged[field]:
                    errors[field] = "entity_required"
            if not errors:
                self._data.update(merged)
                return await self.async_step_assets()

        partial_defaults = {
            field: (
                self._discovery_candidates.get(field, [DEFAULT_ENTITY_MAPPINGS.get(field, "")])[0]
                if self._discovery_candidates.get(field)
                else DEFAULT_ENTITY_MAPPINGS.get(field, "")
            )
            for field in missing
        }
        return self.async_show_form(
            step_id="entities_partial",
            data_schema=_entity_schema(missing, partial_defaults),
            errors=errors,
            description_placeholders={
                "missing_count": str(len(missing)),
            },
        )

    # -----------------------------------------------------------------------
    # Step 5c: No entities found, show full manual form
    # -----------------------------------------------------------------------

    async def async_step_entities_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """No HAEO entities detected. Show full manual entity mapping form."""
        errors: dict[str, str] = {}
        all_fields = list(DEFAULT_ENTITY_MAPPINGS.keys())

        if user_input is not None:
            for field in all_fields:
                val = user_input.get(field, "")
                if not val:
                    errors[field] = "entity_required"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_assets()

        return self.async_show_form(
            step_id="entities_manual",
            data_schema=_entity_schema(all_fields, DEFAULT_ENTITY_MAPPINGS),
            errors=errors,
            description_placeholders={
                "haeo_repo": "https://github.com/hass-energy/haeo",
            },
        )

    # -----------------------------------------------------------------------
    # Step 6: Assets summary and capacity configuration
    # -----------------------------------------------------------------------

    async def async_step_assets(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show discovered assets and ask for EV and battery capacity.

        Displays a read-only summary of the assets in ASSET_DEFAULTS, shows
        any unmapped entities from the global sweep, and collects capacity
        values for home_battery, ev1, and ev2 (since HAEO may not expose them).

        A 'Some of my assets are missing' path is not yet a separate step in
        v0.3 but the unmapped_entities list is stored in the config data for
        the coordinator to surface.
        """
        if user_input is not None:
            # Save capacity values and unmapped entity list
            self._data[CONF_HOME_BATTERY_CAPACITY_KWH] = float(
                user_input.get(CONF_HOME_BATTERY_CAPACITY_KWH, 13.5)
            )
            self._data[CONF_EV1_CAPACITY_KWH] = float(
                user_input.get(CONF_EV1_CAPACITY_KWH, 75.0)
            )
            self._data[CONF_EV2_CAPACITY_KWH] = float(
                user_input.get(CONF_EV2_CAPACITY_KWH, 60.0)
            )
            # Store unmapped entity list for coordinator to surface
            self._data["unmapped_entities"] = self._unmapped_entities
            return await self.async_step_consent()

        # Build asset summary for description_placeholders
        asset_lines = []
        for asset_id, spec in ASSET_DEFAULTS.items():
            asset_lines.append(
                f"{asset_id} ({spec['kind']}): "
                f"soc={spec.get('soc_entity', 'n/a')}, "
                f"setpoint={spec.get('setpoint_entity', 'n/a')}"
            )
        asset_summary = "\n".join(asset_lines)

        unmapped_summary = (
            ", ".join(self._unmapped_entities) if self._unmapped_entities else "none"
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOME_BATTERY_CAPACITY_KWH, default=13.5
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=500.0)),
                vol.Required(
                    CONF_EV1_CAPACITY_KWH, default=75.0
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=500.0)),
                vol.Required(
                    CONF_EV2_CAPACITY_KWH, default=60.0
                ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=500.0)),
            }
        )
        return self.async_show_form(
            step_id="assets",
            data_schema=schema,
            description_placeholders={
                "asset_summary": asset_summary,
                "unmapped_entities": unmapped_summary,
            },
        )

    # -----------------------------------------------------------------------
    # Step 7: Consent (CC-BY-4.0 + cohort participation)
    # -----------------------------------------------------------------------

    async def async_step_consent(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Privacy opt-in and CC-BY-4.0 licence agreement."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_LICENCE_AGREED):
                errors[CONF_LICENCE_AGREED] = "licence_must_be_agreed"
            else:
                self._data.update(user_input)
                self._data[CONF_CONSENT_TIMESTAMP] = datetime.now(tz=UTC).isoformat()

                if self._is_reauth and self._reauth_entry_id:
                    existing = self.hass.config_entries.async_get_entry(
                        self._reauth_entry_id
                    )
                    if existing:
                        self.hass.config_entries.async_update_entry(
                            existing,
                            data={**existing.data, CONF_TOKEN: self._data[CONF_TOKEN]},
                        )
                        await self.hass.config_entries.async_reload(
                            self._reauth_entry_id
                        )
                    return self.async_abort(reason="reauth_successful")

                return self.async_create_entry(
                    title=f"NEM Flex Telemetry ({self._data[CONF_HOUSEHOLD_ID]})",
                    data=self._data,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_OPT_IN_COHORT, default=True): bool,
                vol.Required(CONF_LICENCE_AGREED, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="consent",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                "privacy_url": (
                    "https://github.com/purcell-lab/nem-flex-telemetry/blob/main/SCHEMA.md"
                    "#privacy-and-governance"
                ),
            },
        )

    # -----------------------------------------------------------------------
    # Step 8: Auth error (retry or abort)
    # -----------------------------------------------------------------------

    async def async_step_auth_error(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the Device Flow error with options to retry or abort."""
        if user_input is not None:
            if user_input.get("retry"):
                if hasattr(self, "_poll_task_started"):
                    del self._poll_task_started
                return await self.async_step_device_auth()
            return self.async_abort(reason="auth_error_aborted")

        return self.async_show_form(
            step_id="auth_error",
            data_schema=vol.Schema({vol.Optional("retry", default=True): bool}),
            description_placeholders={
                "error_detail": self._auth_error,
            },
        )

    # -----------------------------------------------------------------------
    # Re-authentication flow
    # -----------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Entry point for re-authentication triggered by the coordinator."""
        self._is_reauth = True
        self._data.update(entry_data)
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_GITHUB_LOGIN) == entry_data.get(CONF_GITHUB_LOGIN):
                self._reauth_entry_id = entry.entry_id
                break
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm re-authentication and kick off Device Flow."""
        if user_input is not None:
            if hasattr(self, "_poll_task_started"):
                del self._poll_task_started
            return await self.async_step_device_auth()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "github_login": self._data.get(CONF_GITHUB_LOGIN, ""),
            },
        )

    # -----------------------------------------------------------------------
    # Options flow registration
    # -----------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "NemFlexTelemetryOptionsFlow":
        """Return the options flow handler."""
        return NemFlexTelemetryOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow: entity remapping without re-entering credentials
# ---------------------------------------------------------------------------


class NemFlexTelemetryOptionsFlow(config_entries.OptionsFlow):
    """Options flow to update entity mappings without re-entering credentials."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options: update entity mappings with EntitySelector dropdowns."""
        errors: dict[str, str] = {}
        current = self.config_entry.data
        all_fields = list(DEFAULT_ENTITY_MAPPINGS.keys())

        if user_input is not None:
            for field in all_fields:
                if not user_input.get(field, ""):
                    errors[field] = "entity_required"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        defaults = {
            field: current.get(field, DEFAULT_ENTITY_MAPPINGS.get(field, ""))
            for field in all_fields
        }
        return self.async_show_form(
            step_id="init",
            data_schema=_entity_schema(all_fields, defaults),
            errors=errors,
        )
