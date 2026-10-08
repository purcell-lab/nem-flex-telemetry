"""Relay records published by Nimbus (#27, nimbus#1634).

Nimbus builds a complete schema v2.0 record and publishes it, whole, on
``sensor.nimbus_flex_telemetry`` under the single ``record`` attribute. This
module only decides whether that record can be relayed. It never rebuilds or
edits a field: Nimbus owns the measurement, this integration owns GitHub
authentication, the household ID, the buffer and the push.

Kept free of Home Assistant imports so the contract is testable directly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .const import NIMBUS_FLEX_SWITCH, NIMBUS_TELEMETRY_ENTITY, SCHEMA_VERSION


@dataclass(frozen=True)
class NimbusRecord:
    """Outcome of reading the Nimbus sensor.

    ``record`` is a deep copy ready to validate and buffer, or None with a
    ``reason`` a user can act on.
    """

    record: dict[str, Any] | None = None
    reason: str | None = None


def read_nimbus_record(
    state: Any,
    *,
    region: str,
    postcode_prefix: str,
) -> NimbusRecord:
    """Return the record on a Nimbus state object, or why it cannot be used.

    ``state`` is a Home Assistant ``State`` (or anything with ``.state`` and
    ``.attributes``), or None when the entity does not exist.

    Region and postcode prefix are checked, not overwritten: the record says
    where Nimbus thinks the household is, and if that disagrees with this
    entry's configuration the safe action is to refuse it (nimbus#1634 step 4).
    """
    if state is None:
        return NimbusRecord(
            reason=f"{NIMBUS_TELEMETRY_ENTITY} not found; is Nimbus installed?"
        )
    attributes = getattr(state, "attributes", {}) or {}
    record = attributes.get("record")
    if not isinstance(record, dict):
        nimbus_reason = attributes.get("reason")
        if nimbus_reason:
            return NimbusRecord(reason=f"Nimbus built no record: {nimbus_reason}")
        return NimbusRecord(
            reason=(
                "Nimbus built no record. Nimbus only builds one while "
                f"{NIMBUS_FLEX_SWITCH} is on (flex ranging)."
            )
        )
    if record.get("schema_version") != SCHEMA_VERSION:
        return NimbusRecord(
            reason=(
                f"Nimbus record schema_version {record.get('schema_version')!r} "
                f"does not match this integration's {SCHEMA_VERSION!r}"
            )
        )
    if not record.get("interval_start_utc"):
        return NimbusRecord(reason="Nimbus record has no interval_start_utc")
    if record.get("region") != region:
        return NimbusRecord(
            reason=(
                f"Nimbus region {record.get('region')!r} does not match the "
                f"configured {region!r}; record not relayed"
            )
        )
    if record.get("postcode_prefix") != postcode_prefix:
        return NimbusRecord(
            reason=(
                "Nimbus postcode prefix does not match the configured prefix; "
                "record not relayed"
            )
        )
    return NimbusRecord(record=copy.deepcopy(record))
