"""Config flow for NEM Flex Telemetry integration.

Flow steps (initial setup):
  1. async_step_user              -- landing page, explains Device Flow
  2. async_step_device_auth       -- requests device code from GitHub
  3. async_step_show_code         -- shows user_code, polls for token in background
  4. async_step_identity          -- household ID, postcode prefix, NEM region
  5. async_step_entities_confirm  -- all HAEO entities auto-detected (confirm or customise)
     async_step_entities_partial  -- some entities missing (pre-filled + missing fields)
     async_step_entities_manual   -- no HAEO detected (full manual form)
  6. async_step_assets            -- how many home batteries, EVs and bidirectional
                                     chargers; unmapped entity report
     async_step_asset_battery     -- one step per battery: capacity + entity mapping
     async_step_asset_ev          -- one step per EV: capacity + entity mapping +
                                     bidirectional charger access
  7. async_step_consent           -- CC-BY-4.0 licence + cohort participation
  8. async_step_auth_error        -- Device Flow failure with retry/abort options

Re-authentication flow (triggered when the coordinator detects a 401):
  R1. async_step_reauth           -- entry point registered by HA
  R2. async_step_reauth_confirm   -- skip straight to device_auth -> show_code -> done
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ASSET_KIND_BATTERY,
    ASSET_KIND_EV,
    CONF_ASSET_BIDIRECTIONAL,
    CONF_ASSET_CAPACITY_KWH,
    CONF_ASSET_SETPOINT_ENTITY,
    CONF_ASSET_SHADOW_ENTITY,
    CONF_ASSET_SOC_ENTITY,
    CONF_ASSETS,
    CONF_BIDIRECTIONAL_CHARGERS,
    CONF_CONSENT_TIMESTAMP,
    CONF_EDIT_ASSETS,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_PRICE_EXPORT,
    CONF_ENTITY_SHADOW_ENERGY,
    CONF_ENTITY_SHADOW_ENVELOPE_EXPORT,
    CONF_ENTITY_SHADOW_ENVELOPE_IMPORT,
    CONF_ENTITY_SHADOW_LOAD_FORECAST,
    CONF_ENTITY_SHADOW_SOLAR_FORECAST,
    CONF_ENTITY_SOLAR,
    CONF_ENTITY_TOTAL_LOAD,
    CONF_EV_COUNT,
    CONF_GITHUB_LOGIN,
    CONF_HOME_BATTERY_COUNT,
    CONF_HOUSEHOLD_ID,
    CONF_LICENCE_AGREED,
    CONF_OPT_IN_COHORT,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    CONF_SOURCE,
    DEFAULT_BIDIRECTIONAL_CHARGERS,
    DEFAULT_ENTITY_MAPPINGS,
    DOMAIN,
    GITHUB_REPO,
    MAX_ASSETS_PER_KIND,
    MAX_BIDIRECTIONAL_CHARGERS,
    NIMBUS_TELEMETRY_ENTITY,
    SOURCE_HAEO,
    SOURCE_NIMBUS,
    SOURCES,
    ENTITY_SELECTOR_DOMAINS,
    REQUIRED_ENTITY_FIELDS,
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
from .github_client import NemFlexGitHubClient
from .assets import asset_id_for, configured_assets, normalise_asset
from .discovery import (
    build_entity_map,
    classify_discovery_result,
    discover_asset_counts,
    discover_asset_entities,
    discover_haeo_entities,
    run_global_sweep,
)

_LOGGER = logging.getLogger(__name__)

# Validation patterns
#
# v0.4 prep change: ``household_id`` is now treated as an *anonymous*
# pseudonymous identifier rather than a lowercase slug. The integration
# defaults the field to a randomly-generated UUID v4 at install time so
# the ID has no link to any real-world identity, location label, or
# GitHub username. Users may override the default with any non-empty
# string they prefer (e.g. they may want a memorable label for their
# own records), so we relax validation accordingly.
#
# Reasoning:
#   * UUID v4 gives ~122 bits of entropy and is collision-free across
#     any plausible cohort size.
#   * Lowercase-slug validation rejected uppercase / underscores /
#     longer-than-64 strings, which is too restrictive once the field
#     is purely opaque from the platform's perspective.
#   * The field is still trimmed and length-bounded to prevent absurd
#     values (e.g. multi-megabyte strings) from breaking storage
#     paths and commit messages downstream.
HOUSEHOLD_ID_MAX_LEN = 128
POSTCODE_PREFIX_RE = re.compile(r"^[0-9]{3}$")

# EntitySelector (gives users a dropdown picker).
# HAEO exposes several inputs as number.* entities (number.solar_forecast,
# number.grid_import_price, number.grid_import_limit), so the selector must
# accept those domains as well as sensor.*. A sensor-only selector rejected
# the discovery defaults on submit.
_ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        domain=list(ENTITY_SELECTOR_DOMAINS), multiple=False
    )
)


def _validate_household_id(value: str) -> str:
    """Validate the anonymous household identifier.

    Accepts any non-empty string up to ``HOUSEHOLD_ID_MAX_LEN`` characters
    after stripping surrounding whitespace. The default value generated by
    the config flow is a UUID v4, but users may override with any string
    they prefer (the field is opaque to the platform and only used as a
    bucket key in storage paths and commit messages).
    """
    if value is None:
        raise vol.Invalid("Household ID must not be empty.")
    stripped = str(value).strip()
    if not stripped:
        raise vol.Invalid("Household ID must not be empty.")
    if len(stripped) > HOUSEHOLD_ID_MAX_LEN:
        raise vol.Invalid(
            f"Household ID must be at most {HOUSEHOLD_ID_MAX_LEN} characters."
        )
    return stripped


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


def _plan_asset_ids(
    kind: str, count: int, previous: list[dict[str, Any]]
) -> list[str]:
    """Pick asset_ids for ``count`` assets of one kind.

    Existing assets of that kind keep their ids (and so their mappings) in
    order; new ones take the next free ``asset_id_for`` slot.
    """
    ids = [a["asset_id"] for a in previous if a.get("kind") == kind][:count]
    index = 1
    while len(ids) < count:
        candidate = asset_id_for(kind, index)
        if candidate not in ids:
            ids.append(candidate)
        index += 1
    return ids


def _asset_counts_schema(batteries: int, evs: int, chargers: int) -> vol.Schema:
    """Form asking how many batteries, EVs and bidirectional chargers (#15)."""
    count = vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_ASSETS_PER_KIND))
    return vol.Schema(
        {
            vol.Required(CONF_HOME_BATTERY_COUNT, default=batteries): count,
            vol.Required(CONF_EV_COUNT, default=evs): count,
            vol.Required(CONF_BIDIRECTIONAL_CHARGERS, default=chargers): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_BIDIRECTIONAL_CHARGERS)
            ),
        }
    )


class _AssetStepsMixin:
    """Per-asset mapping steps shared by the config and options flows (#15).

    ``_async_start_asset_steps`` queues one step per asset; each step asks for
    the capacity and the SOC / setpoint / shadow entities. A capacity of 0 (or
    blank) means the asset does not exist and it is left out. When the queue
    is empty ``_async_assets_done`` receives the asset list.
    """

    _asset_queue: list[tuple[str, str]]
    _assets_out: list[dict[str, Any]]
    _assets_prev: dict[str, dict[str, Any]]
    _chargers: int

    async def _async_assets_done(
        self, assets: list[dict[str, Any]], chargers: int
    ) -> FlowResult:
        raise NotImplementedError

    async def _async_start_asset_steps(
        self, user_input: dict[str, Any], previous: list[dict[str, Any]]
    ) -> FlowResult:
        """Queue the asset steps for the counts the user chose."""
        self._chargers = int(user_input[CONF_BIDIRECTIONAL_CHARGERS])
        self._assets_prev = {a["asset_id"]: a for a in previous}
        self._assets_out = []
        self._asset_queue = [
            (asset_id, kind)
            for kind, key in (
                (ASSET_KIND_BATTERY, CONF_HOME_BATTERY_COUNT),
                (ASSET_KIND_EV, CONF_EV_COUNT),
            )
            for asset_id in _plan_asset_ids(kind, int(user_input[key]), previous)
        ]
        return await self._async_next_asset_step()

    async def _async_next_asset_step(self) -> FlowResult:
        if not self._asset_queue:
            return await self._async_assets_done(self._assets_out, self._chargers)
        if self._asset_queue[0][1] == ASSET_KIND_EV:
            return await self.async_step_asset_ev()
        return await self.async_step_asset_battery()

    async def async_step_asset_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Capacity and entity mapping for one home battery."""
        return await self._async_asset_step("asset_battery", user_input)

    async def async_step_asset_ev(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Capacity, entity mapping and charger access for one EV."""
        return await self._async_asset_step("asset_ev", user_input)

    async def _async_asset_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> FlowResult:
        asset_id, kind = self._asset_queue[0]
        errors: dict[str, str] = {}

        if user_input is not None:
            capacity = float(user_input.get(CONF_ASSET_CAPACITY_KWH) or 0.0)
            if capacity > 0 and not user_input.get(CONF_ASSET_SOC_ENTITY):
                errors[CONF_ASSET_SOC_ENTITY] = "entity_required"
            else:
                if capacity > 0:
                    if not self._chargers:
                        user_input[CONF_ASSET_BIDIRECTIONAL] = False
                    self._assets_out.append(
                        normalise_asset(asset_id, kind, capacity, user_input)
                    )
                self._asset_queue.pop(0)
                return await self._async_next_asset_step()

        # Pre-fill from the previous mapping (options flow) or from the
        # discovery hints that exist on this instance.
        current = self._assets_prev.get(asset_id) or discover_asset_entities(
            self.hass, asset_id
        )
        current = {**current, **(user_input or {})}

        def _suggested(key: str) -> dict[str, Any] | None:
            value = current.get(key)
            return {"suggested_value": value} if value else None

        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_ASSET_CAPACITY_KWH,
                description=_suggested(CONF_ASSET_CAPACITY_KWH),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=500.0)),
        }
        for key in (CONF_ASSET_SOC_ENTITY, CONF_ASSET_SETPOINT_ENTITY, CONF_ASSET_SHADOW_ENTITY):
            schema[vol.Optional(key, description=_suggested(key))] = _ENTITY_SELECTOR
        if kind == ASSET_KIND_EV and self._chargers:
            schema[
                vol.Required(
                    CONF_ASSET_BIDIRECTIONAL,
                    default=bool(current.get(CONF_ASSET_BIDIRECTIONAL, True)),
                )
            ] = bool

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "asset_id": asset_id,
                "chargers": str(self._chargers),
            },
        )


class NemFlexTelemetryConfigFlow(
    _AssetStepsMixin, config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle the NEM Flex Telemetry config flow.

    Guides the user through GitHub Device Flow authorisation, household
    identity capture, HAEO entity auto-discovery (with manual fallback),
    asset configuration, and consent.
    """

    # v4 (#15): variable asset list under CONF_ASSETS.
    VERSION = 4

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._data: dict[str, Any] = {}
        self._device_flow: dict[str, Any] = {}
        self._auth_error: str = ""
        self._discovery_best: dict[str, str | None] = {}
        self._discovery_candidates: dict[str, list[str]] = {}
        self._unmapped_entities: list[str] = []
        self._is_reauth: bool = False
        # Note: cannot use ``_reauth_entry_id`` because recent Home Assistant
        # versions expose that name as a read-only property on the base
        # ``ConfigFlow`` class. Use a distinct attribute name here.
        self._reauth_entry_ref: str | None = None

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
        """Show the user code and poll for the access token in the background.

        Uses the modern ``progress_task`` pattern so Home Assistant wakes the
        flow up the moment the polling task completes, rather than relying on
        the deprecated ``async_show_progress_done`` call from inside the task.
        """
        # Start the polling task on first entry into this step.
        if not hasattr(self, "_poll_task") or self._poll_task is None:
            self._poll_task = self.hass.async_create_task(self._poll_for_token())

        # If the task has finished, advance to the appropriate next step.
        if self._poll_task.done():
            # Surface any unexpected exception so it ends up in the HA log
            # rather than vanishing silently.
            exc = self._poll_task.exception()
            if exc is not None:
                _LOGGER.error("Device flow polling task failed: %s", exc)
                self._auth_error = "network"
                return self.async_show_progress_done(next_step_id="auth_error")

            next_step = getattr(self, "_poll_next_step", "auth_error")
            return self.async_show_progress_done(next_step_id=next_step)

        # Still polling: show the progress dialog. ``progress_task`` makes HA
        # re-invoke this step automatically when the task completes.
        return self.async_show_progress(
            step_id="show_code",
            progress_action="waiting_for_user",
            progress_task=self._poll_task,
            description_placeholders={
                "user_code": self._device_flow.get("user_code", ""),
                "verification_uri": "https://github.com/login/device",
                "verification_uri_complete": self._device_flow.get(
                    "verification_uri_complete", ""
                ),
            },
        )

    async def _poll_for_token(self) -> None:
        """Background task: poll GitHub for the access token.

        Stores the next step id on ``self._poll_next_step`` so
        ``async_step_show_code`` can route to it once the task completes.
        Does not call ``async_show_progress_done`` from inside the task,
        which is the cause of the dialog hanging on success in newer
        Home Assistant versions.
        """
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
            # Check write access now, not at the first hourly push (#14).
            # None means GitHub could not say; carry on as before.
            can_push = await NemFlexGitHubClient(
                token=token, repo_name=GITHUB_REPO
            ).has_push_access()
            self._poll_next_step = "identity" if can_push is not False else "no_push_access"
        except DeviceFlowExpired:
            self._auth_error = "device_flow_expired"
            self._poll_next_step = "auth_error"
        except DeviceFlowDenied:
            self._auth_error = "device_flow_denied"
            self._poll_next_step = "auth_error"
        except DeviceFlowInvalid:
            self._auth_error = "device_flow_invalid"
            self._poll_next_step = "auth_error"
        except DeviceFlowNetworkError as exc:
            self._auth_error = str(exc)
            self._poll_next_step = "auth_error"

    async def async_step_no_push_access(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Explain that the account cannot write to the telemetry repo (#14).

        The user can continue: records are buffered and pushed once the
        maintainer grants access, and a repair issue is raised meanwhile.
        """
        if user_input is not None:
            return await self.async_step_identity()
        return self.async_show_form(
            step_id="no_push_access",
            data_schema=vol.Schema({}),
            description_placeholders={
                "github_login": self._data.get(CONF_GITHUB_LOGIN, ""),
                "repo": GITHUB_REPO,
                "issues_url": "https://github.com/purcell-lab/nem-flex-telemetry/issues/new",
            },
        )

    # -----------------------------------------------------------------------
    # Step 4: Household identity
    # -----------------------------------------------------------------------

    async def async_step_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect household ID, postcode prefix, and NEM region.

        ``household_id`` is pre-populated with a freshly generated UUID v4
        so privacy-conscious users can accept the default without having
        to invent a slug. Users who prefer a memorable label may overwrite
        the field with any non-empty string up to ``HOUSEHOLD_ID_MAX_LEN``
        characters. See ``docs/PRIVACY.md`` for the threat model and the
        rationale behind treating this field as a pseudonym.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                normalised_id = _validate_household_id(
                    user_input.get(CONF_HOUSEHOLD_ID, "")
                )
                user_input[CONF_HOUSEHOLD_ID] = normalised_id
            except vol.Invalid:
                errors[CONF_HOUSEHOLD_ID] = "invalid_household_id"

            try:
                _validate_postcode_prefix(user_input[CONF_POSTCODE_PREFIX])
            except vol.Invalid:
                errors[CONF_POSTCODE_PREFIX] = "invalid_postcode_prefix"

            if not errors:
                self._data.update(user_input)
                return await self._async_step_entities_start()

        # Generate a fresh UUID v4 default on each form render so the user
        # always sees a unique, unattributable identifier suggestion. We
        # only generate it if no value has been entered yet (so a previous
        # validation error does not silently replace what the user typed).
        default_household_id = (
            (user_input or {}).get(CONF_HOUSEHOLD_ID) or str(uuid.uuid4())
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOUSEHOLD_ID, default=default_household_id
                ): str,
                vol.Required(
                    CONF_POSTCODE_PREFIX,
                    default=(user_input or {}).get(CONF_POSTCODE_PREFIX, ""),
                ): str,
                vol.Required(
                    CONF_REGION,
                    default=(user_input or {}).get(CONF_REGION, "QLD1"),
                ): vol.In(NEM_REGIONS),
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
        """Ask how many home batteries, EVs and bidirectional chargers (#15).

        Any of the three may be 0. The counts are pre-filled from the assets
        whose hinted entities exist on this instance; one asset step per
        battery and EV follows. Any unmapped entities from the global sweep
        are listed so the user can spot asset entities with unusual names.
        """
        if user_input is not None:
            return await self._async_start_asset_steps(user_input, previous=[])

        batteries, evs, chargers = discover_asset_counts(self.hass)
        unmapped_summary = (
            ", ".join(self._unmapped_entities) if self._unmapped_entities else "none"
        )
        return self.async_show_form(
            step_id="assets",
            data_schema=_asset_counts_schema(batteries, evs, chargers),
            description_placeholders={
                "unmapped_entities": unmapped_summary,
            },
        )

    async def _async_assets_done(
        self, assets: list[dict[str, Any]], chargers: int
    ) -> FlowResult:
        """Store the asset list and continue to consent."""
        self._data[CONF_ASSETS] = assets
        self._data[CONF_BIDIRECTIONAL_CHARGERS] = chargers
        # Store unmapped entity list for coordinator to surface
        self._data["unmapped_entities"] = self._unmapped_entities
        return await self.async_step_consent()

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

                if self._is_reauth and self._reauth_entry_ref:
                    existing = self.hass.config_entries.async_get_entry(
                        self._reauth_entry_ref
                    )
                    if existing:
                        self.hass.config_entries.async_update_entry(
                            existing,
                            data={**existing.data, CONF_TOKEN: self._data[CONF_TOKEN]},
                        )
                        await self.hass.config_entries.async_reload(
                            self._reauth_entry_ref
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
                self._reauth_entry_ref = entry.entry_id
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
        # Home Assistant (2024.11+) supplies ``self.config_entry`` on the
        # options flow. Do not pass or assign it: recent releases make it a
        # read-only property and assignment raises AttributeError (#16).
        return NemFlexTelemetryOptionsFlow()


# ---------------------------------------------------------------------------
# Options flow: entity remapping without re-entering credentials
# ---------------------------------------------------------------------------


class NemFlexTelemetryOptionsFlow(_AssetStepsMixin, config_entries.OptionsFlow):
    """Options flow to update entity mappings and the asset list.

    Values are written to ``entry.options``. The coordinator reads the merged
    view ``{**entry.data, **entry.options}`` (see ``entry_config``), and an
    update listener reloads the entry so changes take effect immediately.

    Every entity field is written explicitly. A cleared optional field is
    stored as ``None`` so it overrides the original value in ``entry.data``
    instead of silently falling back to it.

    The asset list is carried over unchanged unless "Edit batteries and
    EVs" is ticked, which leads through the same asset steps as setup (#15).
    """

    _pending_options: dict[str, Any]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show and save entity mappings; optionally go on to the assets."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        entity_fields = list(DEFAULT_ENTITY_MAPPINGS.keys())

        if user_input is not None:
            source = user_input.get(CONF_SOURCE, SOURCE_HAEO)
            if source == SOURCE_NIMBUS:
                # Nimbus supplies the whole record, so entity mappings are
                # not needed; the sensor must exist (#27).
                if self.hass.states.get(NIMBUS_TELEMETRY_ENTITY) is None:
                    errors[CONF_SOURCE] = "nimbus_not_found"
            else:
                for field in REQUIRED_ENTITY_FIELDS:
                    if not user_input.get(field):
                        errors[field] = "entity_required"
            if not errors:
                new_options: dict[str, Any] = {
                    field: (user_input.get(field) or None)
                    for field in entity_fields
                }
                new_options[CONF_SOURCE] = source
                # Options are replaced wholesale on save, so carry the asset
                # list over explicitly.
                saved = {**self.config_entry.data, **self.config_entry.options}
                new_options[CONF_ASSETS] = configured_assets(saved)
                new_options[CONF_BIDIRECTIONAL_CHARGERS] = int(
                    saved.get(CONF_BIDIRECTIONAL_CHARGERS, DEFAULT_BIDIRECTIONAL_CHARGERS)
                )
                if user_input.get(CONF_EDIT_ASSETS):
                    self._pending_options = new_options
                    return await self.async_step_assets()
                return self.async_create_entry(title="", data=new_options)
            # Re-show the form with what the user just entered.
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(entity_fields, current),
            errors=errors,
        )

    async def async_step_assets(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask how many batteries, EVs and bidirectional chargers (#15)."""
        previous = self._pending_options[CONF_ASSETS]
        if user_input is not None:
            return await self._async_start_asset_steps(user_input, previous)

        return self.async_show_form(
            step_id="assets",
            data_schema=_asset_counts_schema(
                sum(1 for a in previous if a.get("kind") == ASSET_KIND_BATTERY),
                sum(1 for a in previous if a.get("kind") == ASSET_KIND_EV),
                self._pending_options[CONF_BIDIRECTIONAL_CHARGERS],
            ),
        )

    async def _async_assets_done(
        self, assets: list[dict[str, Any]], chargers: int
    ) -> FlowResult:
        """Save the mappings from the first step with the new asset list."""
        return self.async_create_entry(
            title="",
            data={
                **self._pending_options,
                CONF_ASSETS: assets,
                CONF_BIDIRECTIONAL_CHARGERS: chargers,
            },
        )


def _options_schema(entity_fields: list[str], current: dict[str, Any]) -> vol.Schema:
    """Build the options form.

    Required entity fields use ``vol.Required``; the rest use ``vol.Optional``
    so they can be left blank. ``suggested_value`` pre-fills the form without
    forcing a default back in when the user clears a field.
    """
    schema: dict[Any, Any] = {
        vol.Required(CONF_SOURCE, default=current.get(CONF_SOURCE) or SOURCE_HAEO): (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(SOURCES),
                    translation_key=CONF_SOURCE,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        )
    }
    for field in entity_fields:
        # Every mapping is Optional in the form so the user can switch the
        # source to Nimbus in one step (#27). With the HAEO source, the
        # required fields are enforced in async_step_init instead.
        marker = vol.Optional
        value = current.get(field)
        description = {"suggested_value": value} if value else None
        schema[marker(field, description=description)] = _ENTITY_SELECTOR
    schema[vol.Optional(CONF_EDIT_ASSETS, default=False)] = bool
    return vol.Schema(schema)
