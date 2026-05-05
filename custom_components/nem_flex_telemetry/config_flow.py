"""Config flow for NEM Flex Telemetry integration.

Three steps:
  1. GitHub credentials + household identity
  2. HAEO entity mappings
  3. Privacy opt-in and licence agreement
"""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_ENTITY_BASELINE,
    CONF_ENTITY_ENVELOPE_EXPORT,
    CONF_ENTITY_ENVELOPE_IMPORT,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONF_ENTITY_NET_IMPORT,
    CONF_ENTITY_PRICE_SIGNAL,
    CONF_ENTITY_SETPOINT,
    CONF_ENTITY_SOC,
    CONF_GITHUB_PAT,
    CONF_HOUSEHOLD_ID,
    CONF_LICENCE_AGREED,
    CONF_OPT_IN_COHORT,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    DEFAULT_ENTITY_MAPPINGS,
    DOMAIN,
    NEM_REGIONS,
)

_LOGGER = logging.getLogger(__name__)

# Regex for valid household ID slugs
HOUSEHOLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")
POSTCODE_PREFIX_RE = re.compile(r"^[0-9]{3}$")


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


STEP_1_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GITHUB_PAT): str,
        vol.Required(CONF_HOUSEHOLD_ID): vol.All(str, _validate_household_id),
        vol.Required(CONF_POSTCODE_PREFIX): vol.All(str, _validate_postcode_prefix),
        vol.Required(CONF_REGION): vol.In(NEM_REGIONS),
    }
)

STEP_2_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_NET_IMPORT, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_NET_IMPORT]): str,
        vol.Required(CONF_ENTITY_PRICE_SIGNAL, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_PRICE_SIGNAL]): str,
        vol.Required(CONF_ENTITY_SETPOINT, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_SETPOINT]): str,
        vol.Required(CONF_ENTITY_FLEX_UP, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_FLEX_UP]): str,
        vol.Required(CONF_ENTITY_FLEX_DOWN, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_FLEX_DOWN]): str,
        vol.Required(CONF_ENTITY_SOC, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_SOC]): str,
        vol.Required(CONF_ENTITY_ENVELOPE_IMPORT, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_ENVELOPE_IMPORT]): str,
        vol.Required(CONF_ENTITY_ENVELOPE_EXPORT, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_ENVELOPE_EXPORT]): str,
        vol.Required(CONF_ENTITY_BASELINE, default=DEFAULT_ENTITY_MAPPINGS[CONF_ENTITY_BASELINE]): str,
    }
)

STEP_3_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OPT_IN_COHORT, default=True): bool,
        vol.Required(CONF_LICENCE_AGREED, default=False): bool,
    }
)


async def _test_github_pat(hass: HomeAssistant, pat: str, household_id: str) -> dict[str, str]:
    """Test the GitHub PAT by attempting to read the repo.

    Returns an empty dict on success, or a dict with key 'base' containing the error.
    """
    try:
        # Import here to avoid blocking import at module level
        from github import Github, GithubException  # type: ignore[import]

        def _check() -> None:
            g = Github(pat)
            repo = g.get_repo("purcell-lab/nem-flex-telemetry")
            # Verify we can see the repo (read access)
            _ = repo.full_name

        await hass.async_add_executor_job(_check)
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.warning("GitHub PAT validation failed: %s", exc)
        return {"base": "github_auth_failed"}


class NemFlexTelemetryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the NEM Flex Telemetry config flow.

    Step 1: GitHub credentials and household identity.
    Step 2: HAEO entity mappings.
    Step 3: Privacy consent and CC-BY-4.0 licence agreement.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle step 1: GitHub credentials and household identity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _test_github_pat(
                self.hass, user_input[CONF_GITHUB_PAT], user_input[CONF_HOUSEHOLD_ID]
            )
            if not errors:
                self._data.update(user_input)
                return await self.async_step_entities()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_1_SCHEMA,
            errors=errors,
            description_placeholders={
                "pat_docs": "https://github.com/purcell-lab/nem-flex-telemetry/blob/main/docs/INSTALL.md#step-1-create-a-fine-grained-github-pat",
                "regions": ", ".join(NEM_REGIONS),
            },
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle step 2: HAEO entity mappings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Lightly validate that entity IDs look plausible
            for key, entity_id in user_input.items():
                if not entity_id.startswith("sensor."):
                    errors[key] = "entity_must_be_sensor"

            if not errors:
                self._data.update(user_input)
                return await self.async_step_consent()

        return self.async_show_form(
            step_id="entities",
            data_schema=STEP_2_SCHEMA,
            errors=errors,
            description_placeholders={
                "haeo_docs": "https://github.com/hass-energy/haeo",
            },
        )

    async def async_step_consent(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle step 3: Privacy opt-in and CC-BY-4.0 licence agreement."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_LICENCE_AGREED):
                errors[CONF_LICENCE_AGREED] = "licence_must_be_agreed"
            else:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=f"NEM Flex Telemetry ({self._data[CONF_HOUSEHOLD_ID]})",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="consent",
            data_schema=STEP_3_SCHEMA,
            errors=errors,
            description_placeholders={
                "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                "privacy_url": "https://github.com/purcell-lab/nem-flex-telemetry/blob/main/SCHEMA.md#privacy-and-governance",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> NemFlexTelemetryOptionsFlow:
        """Return the options flow handler."""
        return NemFlexTelemetryOptionsFlow(config_entry)


class NemFlexTelemetryOptionsFlow(config_entries.OptionsFlow):
    """Options flow to update entity mappings without re-entering credentials."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options: update entity mappings."""
        errors: dict[str, str] = {}
        current = self.config_entry.data

        if user_input is not None:
            for key, entity_id in user_input.items():
                if not entity_id.startswith("sensor."):
                    errors[key] = "entity_must_be_sensor"
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # Pre-fill with existing entity mappings
        options_schema = vol.Schema(
            {
                vol.Required(k, default=current.get(k, v)): str
                for k, v in DEFAULT_ENTITY_MAPPINGS.items()
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )
