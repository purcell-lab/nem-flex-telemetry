"""Configured asset list for NEM Flex Telemetry (#15).

A household has 0..N home batteries and 0..N EVs. Each one is stored in the
config entry under ``CONF_ASSETS`` as a dict:

    {"asset_id": "ev1", "kind": "ev", "capacity_kwh": 75.0,
     "bidirectional_capable": True,
     "soc_entity": "sensor.ev1_state_of_charge",
     "setpoint_entity": "sensor.ev1_active_power",
     "shadow_entity": "sensor.ev1_power_balance_shadow_price"}

Entries created before #15 (config entry v3) have no asset list, only the
three fixed capacity keys. ``legacy_assets`` rebuilds the old home_battery /
ev1 / ev2 trio from those so the reference install is unchanged.

Kept free of Home Assistant imports so it can be used by the migration, the
config flow and the coordinator alike.
"""

from __future__ import annotations

from typing import Any

from .const import (
    ASSET_DEFAULTS,
    ASSET_KIND_BATTERY,
    ASSET_KIND_EV,
    CONF_ASSETS,
    LEGACY_CAPACITY_KEYS,
    PLACEHOLDER_CAPACITY_KWH,
)

_ENTITY_KEYS = ("soc_entity", "setpoint_entity", "shadow_entity")


def asset_id_for(kind: str, index: int) -> str:
    """Return the asset_id for the index-th (1-based) asset of a kind.

    Matches the reference install for the first ones: ``home_battery``,
    ``ev1``, ``ev2``. Further batteries are ``home_battery_2`` and so on.
    """
    if kind == ASSET_KIND_EV:
        return f"ev{index}"
    return "home_battery" if index == 1 else f"home_battery_{index}"


def asset_entity_hints(asset_id: str) -> dict[str, Any]:
    """Return the discovery hints for an asset_id.

    ASSET_DEFAULTS covers the reference install. Other asset_ids follow
    HAEO's naming, which uses the element name as the entity prefix.
    """
    if asset_id in ASSET_DEFAULTS:
        return dict(ASSET_DEFAULTS[asset_id])
    return {
        "soc_entity": f"sensor.{asset_id}_state_of_charge",
        "setpoint_entity": f"sensor.{asset_id}_active_power",
        "shadow_entity": f"sensor.{asset_id}_power_balance_shadow_price",
    }


def legacy_assets(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild the fixed v3 asset trio from a pre-#15 configuration.

    Entities come from ASSET_DEFAULTS, capacities from the old config keys.
    Assets entered with the 0.1 kWh placeholder are dropped: the v3 form
    forced a value, so 0.1 meant "I do not have this asset".
    """
    assets: list[dict[str, Any]] = []
    for asset_id, spec in ASSET_DEFAULTS.items():
        key = LEGACY_CAPACITY_KEYS[asset_id]
        try:
            capacity = float(config.get(key, spec["capacity_kwh"]))
        except (TypeError, ValueError):
            capacity = float(spec["capacity_kwh"])
        if capacity <= PLACEHOLDER_CAPACITY_KWH:
            continue
        assets.append(
            {
                "asset_id": asset_id,
                "kind": spec["kind"],
                "capacity_kwh": capacity,
                "bidirectional_capable": bool(spec["bidirectional_capable"]),
                **{k: spec.get(k) for k in _ENTITY_KEYS},
            }
        )
    return assets


def configured_assets(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the asset list for an entry's merged config.

    ``CONF_ASSETS`` wins when present, even when empty (a household with no
    battery and no EV). Without it the legacy trio is derived.
    """
    if CONF_ASSETS in config and config[CONF_ASSETS] is not None:
        return [dict(a) for a in config[CONF_ASSETS]]
    return legacy_assets(config)


def normalise_asset(
    asset_id: str, kind: str, capacity_kwh: float, form: dict[str, Any]
) -> dict[str, Any]:
    """Build a stored asset dict from one asset step's form input."""
    return {
        "asset_id": asset_id,
        "kind": kind,
        "capacity_kwh": float(capacity_kwh),
        # A stationary battery always charges and discharges; for an EV the
        # flag says whether it can use one of the bidirectional chargers.
        "bidirectional_capable": (
            True if kind == ASSET_KIND_BATTERY else bool(form.get("bidirectional_capable"))
        ),
        **{k: (form.get(k) or None) for k in _ENTITY_KEYS},
    }
