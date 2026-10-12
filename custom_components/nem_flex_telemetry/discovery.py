"""HAEO entity auto-discovery for NEM Flex Telemetry.

Auto-discovery contract
-----------------------
HAEO entities follow the convention sensor.haeo_<field>, but the canonical
names on the user's live instance (cross-referenced against the ApexCharts
dashboard) are listed in DEFAULT_HAEO_ENTITIES in const.py.

On startup of the config flow, this module scans the running Home Assistant
instance for known entity IDs in priority order. The first match for each
field wins.

In addition, a global sweep (run_global_sweep) walks hass.states.async_all()
matching GLOBAL_SWEEP_PATTERNS and surfaces any entity not already in the
named-entity mapping as 'unmapped_entities'. This sweep is also re-run on
every coordinator startup so newly added HAEO entities are picked up
automatically without requiring the user to reconfigure.

If your HAEO instance uses different entity names, the integration falls back
to a partial or full manual mapping step in the config flow. Contributing
default mappings for other optimisers (EMHASS, etc.) is welcome via PR to
https://github.com/purcell-lab/nem-flex-telemetry.

Discovery results
-----------------
discover_haeo_entities() returns two dicts:

    best: {field_key: entity_id | None}
        The single best entity found for each field (None if nothing matched).

    candidates: {field_key: list[str]}
        All candidate entity_ids that actually exist on this instance, in
        priority order. Used to populate EntitySelector dropdowns in the
        partial and manual override steps.

run_global_sweep() returns:

    unmapped_entities: list[str]
        Entity IDs that match GLOBAL_SWEEP_PATTERNS but are not already covered
        by any named mapping. These are surfaced in the config flow for manual
        association.

discover_asset_counts() / discover_asset_entities() (#15):
    Pre-fill the asset steps. ASSET_DEFAULTS and the HAEO naming convention
    are hints only; nothing is assumed to exist until its entity is found.

discover_context_entities() returns:

    context: {context_key: entity_id | None}
        Best match for each reference-only context entity.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .assets import asset_entity_hints, asset_id_for
from .const import (
    ASSET_DEFAULTS,
    ASSET_KIND_BATTERY,
    ASSET_KIND_EV,
    CONF_ENTITY_FLEX_DOWN,
    CONF_ENTITY_FLEX_UP,
    CONTEXT_ENTITIES,
    ENTITY_DCEV_AC_TO_DC,
    MAX_ASSETS_PER_KIND,
    DEFAULT_HAEO_ENTITIES,
    GLOBAL_SWEEP_PATTERNS,
    REGION_PD7DAY_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

# The Home Assistant integration domain for HAEO
_HAEO_DOMAIN = "haeo"


async def discover_haeo_entities(
    hass: HomeAssistant,
) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    """Scan the HA instance for HAEO entities matching the telemetry schema fields.

    This is a pure state-read: no network calls, no blocking I/O.

    Args:
        hass: The Home Assistant instance.

    Returns:
        A tuple of:
        - best: dict mapping each config key to the best matched entity_id,
          or None if no candidate was found.
        - candidates: dict mapping each config key to a list of all candidate
          entity_ids that actually exist on this instance (in priority order).
    """
    haeo_entries = hass.config_entries.async_entries(_HAEO_DOMAIN)
    if haeo_entries:
        _LOGGER.debug(
            "HAEO integration detected (%d config entr%s).",
            len(haeo_entries),
            "y" if len(haeo_entries) == 1 else "ies",
        )
    else:
        _LOGGER.warning(
            "HAEO integration not detected on this Home Assistant instance. "
            "Entity auto-discovery may fall back to manual mapping. "
            "See https://github.com/hass-energy/haeo to install HAEO."
        )

    best: dict[str, str | None] = {}
    candidates: dict[str, list[str]] = {}

    for field_key, spec in DEFAULT_HAEO_ENTITIES.items():
        candidate_list: list[str] = []
        if spec.get("primary"):
            candidate_list.append(spec["primary"])
        candidate_list.extend(spec.get("fallback", []))

        found_candidates: list[str] = []
        first_match: str | None = None

        for entity_id in candidate_list:
            state = hass.states.get(entity_id)
            if state is not None:
                found_candidates.append(entity_id)
                if first_match is None:
                    first_match = entity_id
                    _LOGGER.debug("Auto-discovered %s -> %s", field_key, entity_id)

        best[field_key] = first_match
        candidates[field_key] = found_candidates

        if first_match is None:
            _LOGGER.debug(
                "No entity found for %s (tried: %s).",
                field_key,
                ", ".join(candidate_list) if candidate_list else "none",
            )

    discovered_count = sum(1 for v in best.values() if v is not None)
    total = len(DEFAULT_HAEO_ENTITIES)
    _LOGGER.info(
        "HAEO entity discovery complete: %d/%d fields matched.", discovered_count, total
    )

    return best, candidates


def run_global_sweep(
    hass: HomeAssistant,
    already_mapped: set[str] | None = None,
    extra_mapped: set[str] | None = None,
) -> list[str]:
    """Sweep all HA states for entities matching GLOBAL_SWEEP_PATTERNS.

    Any entity that matches at least one pattern and is not already in the
    'already_mapped' set is added to the returned 'unmapped_entities' list.

    This is called at config-flow discovery time and again on every coordinator
    startup, so newly added HAEO entities are surfaced without reconfiguring.

    Args:
        hass: The Home Assistant instance.
        already_mapped: Set of entity_ids already assigned to a schema field
                        or asset. If None, defaults to the union of all primary
                        and fallback entities from DEFAULT_HAEO_ENTITIES plus
                        ASSET_DEFAULTS entity references.
        extra_mapped: Further entity_ids to treat as mapped, e.g. the
                      entities of the configured assets.

    Returns:
        Sorted list of entity_ids that matched a sweep pattern but are not
        already mapped.
    """
    if already_mapped is None:
        already_mapped = _build_known_entity_set()
    if extra_mapped:
        already_mapped = already_mapped | extra_mapped

    matched: list[str] = []
    for state in hass.states.async_all():
        eid = state.entity_id
        if eid in already_mapped:
            continue
        for pattern in GLOBAL_SWEEP_PATTERNS:
            if pattern.match(eid):
                matched.append(eid)
                break

    matched.sort()

    if matched:
        _LOGGER.info(
            "Global sweep found %d unmapped entit%s: %s",
            len(matched),
            "y" if len(matched) == 1 else "ies",
            ", ".join(matched),
        )
    else:
        _LOGGER.debug("Global sweep: no unmapped entities found.")

    return matched


def _build_known_entity_set() -> set[str]:
    """Build the set of all entity IDs already referenced in named mappings."""
    known: set[str] = set()
    for spec in DEFAULT_HAEO_ENTITIES.values():
        if spec.get("primary"):
            known.add(spec["primary"])
        known.update(spec.get("fallback", []))
    for asset_spec in ASSET_DEFAULTS.values():
        for key in ("soc_entity", "setpoint_entity", "shadow_entity"):
            val = asset_spec.get(key)
            if val:
                known.add(val)
    return known


def discover_asset_entities(hass: HomeAssistant, asset_id: str) -> dict[str, Any]:
    """Return the hinted entities for an asset that exist on this instance.

    Keys are soc_entity / setpoint_entity / shadow_entity (only those found),
    plus capacity_kwh when the asset is in ASSET_DEFAULTS and its SOC entity
    exists, so the reference install gets its usual capacities pre-filled.
    """
    hints = asset_entity_hints(asset_id)
    found: dict[str, Any] = {
        key: hints[key]
        for key in ("soc_entity", "setpoint_entity", "shadow_entity")
        if hints.get(key) and hass.states.get(hints[key]) is not None
    }
    if "soc_entity" in found and hints.get("capacity_kwh"):
        found["capacity_kwh"] = hints["capacity_kwh"]
    return found


def discover_asset_counts(hass: HomeAssistant) -> tuple[int, int, int]:
    """Suggest (home batteries, EVs, bidirectional chargers) for this instance.

    An asset counts when its hinted SOC entity exists, scanning
    home_battery, home_battery_2, ... and ev1, ev2, ... in order until one
    is missing. One bidirectional charger is suggested when an EV was found
    and the DCEV charger rating entity exists.
    """
    counts: dict[str, int] = {}
    for kind in (ASSET_KIND_BATTERY, ASSET_KIND_EV):
        n = 0
        while n < MAX_ASSETS_PER_KIND:
            soc = asset_entity_hints(asset_id_for(kind, n + 1)).get("soc_entity")
            if not soc or hass.states.get(soc) is None:
                break
            n += 1
        counts[kind] = n
    chargers = (
        1
        if counts[ASSET_KIND_EV] and hass.states.get(ENTITY_DCEV_AC_TO_DC) is not None
        else 0
    )
    _LOGGER.debug(
        "Asset discovery: %d home batter%s, %d EV(s), %d bidirectional charger(s)",
        counts[ASSET_KIND_BATTERY],
        "y" if counts[ASSET_KIND_BATTERY] == 1 else "ies",
        counts[ASSET_KIND_EV],
        chargers,
    )
    return counts[ASSET_KIND_BATTERY], counts[ASSET_KIND_EV], chargers


async def discover_context_entities(
    hass: HomeAssistant,
    region: str | None = None,
) -> dict[str, str | None]:
    """Discover reference-only context entities.

    Args:
        hass: The Home Assistant instance.
        region: The NEM region code from the identity step.

    Returns:
        dict mapping context key to the best matched entity_id or None.
    """
    context: dict[str, str | None] = {}

    for ctx_key, spec in CONTEXT_ENTITIES.items():
        if ctx_key == "regional_price_forecast" and region:
            region_entity = REGION_PD7DAY_ENTITY.get(region.upper())
            if region_entity and hass.states.get(region_entity) is not None:
                context[ctx_key] = region_entity
                _LOGGER.info(
                    "Context entity regional_price_forecast -> %s (region: %s)",
                    region_entity,
                    region,
                )
                continue

        candidate_list: list[str] = []
        if spec.get("primary"):
            candidate_list.append(spec["primary"])
        candidate_list.extend(spec.get("fallback", []))

        first_match: str | None = None
        for entity_id in candidate_list:
            if hass.states.get(entity_id) is not None:
                first_match = entity_id
                break

        context[ctx_key] = first_match
        if first_match:
            _LOGGER.info("Context entity %s -> %s", ctx_key, first_match)
        else:
            _LOGGER.debug(
                "Context entity %s not found (tried: %s).",
                ctx_key,
                ", ".join(candidate_list) if candidate_list else "none",
            )

    return context


def classify_discovery_result(
    best: dict[str, str | None],
) -> tuple[str, list[str]]:
    """Classify the discovery result to drive the config flow branch.

    Args:
        best: The 'best' dict returned by discover_haeo_entities().

    Returns:
        A tuple of:
        - mode: one of "all", "partial", "none"
        - missing: list of field keys with no match (empty when mode == "all")
    """
    missing = [k for k, v in best.items() if v is None]
    if not missing:
        return "all", []
    if len(missing) == len(best):
        return "none", missing
    return "partial", missing


def build_entity_map(
    best: dict[str, str | None],
    overrides: dict[str, Any],
) -> dict[str, str]:
    """Merge auto-discovered entities with manual overrides from the form.

    Args:
        best: The 'best' dict returned by discover_haeo_entities().
        overrides: User-submitted form data (may be a subset of fields).

    Returns:
        A merged dict of {field_key: entity_id}.
    """
    result: dict[str, str] = {}
    for field_key in best:
        override = overrides.get(field_key)
        if override:
            result[field_key] = override
        elif best[field_key] is not None:
            result[field_key] = best[field_key]  # type: ignore[assignment]
    return result
