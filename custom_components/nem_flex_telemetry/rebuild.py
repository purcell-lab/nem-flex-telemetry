"""Rebuild missed telemetry intervals from Home Assistant recorder history.

An interval can be missing from GitHub because Home Assistant restarted, a
push failed for longer than the buffer holds, a required input was briefly
unavailable, or the record failed validation (the $20/kWh price window,
fixed in #37). The recorder keeps the entity history that the live builder
read, so those intervals can be rebuilt for as long as the recorder retains
it (``purge_keep_days``, 10 days by default).

How an interval is rebuilt (HAEO source):

- The live coordinator reads every entity once per interval, at a fixed
  offset into the interval (the coordinator's 5-minute timer is not aligned
  to the clock). That offset is saved; a rebuilt interval reads each entity's
  state as it was at ``interval_start + offset``.
- The same ``RecordBuilder`` field logic builds the record, fed by a history
  lookup instead of ``hass.states``. The required-input check and
  ``_validate_record`` apply exactly as for a live record.
- EV connection state, the sticky bidirectional charger owner and the
  recent-discharge window depend on earlier intervals. Each run of missing
  intervals is replayed in time order from ``REPLAY_WARMUP`` before its first
  interval, including the intervals around it that are already published, so
  those values come from the historical sequence.

Nimbus source: Nimbus publishes the whole record on
``sensor.nimbus_flex_telemetry`` (attribute ``record``), so a missing
interval is taken from the attribute history of that sensor.

Only module-level helpers live here. The coordinator owns the orchestration
because it owns the buffer, the GitHub client and the persisted state.
Home Assistant's recorder is imported lazily so this module imports without
it.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from .const import UPDATE_INTERVAL_SECONDS
from .record_builder import (
    _BIDIRECTIONAL_STICKY_HOURS,
    RecordBuilder,
    interval_start_for,
)

_LOGGER = logging.getLogger(__name__)

INTERVAL = timedelta(seconds=UPDATE_INTERVAL_SECONDS)

# Replay this far before each run of missing intervals so EV SOC deltas and
# the sticky bidirectional window are derived from history, not reset.
REPLAY_WARMUP = timedelta(hours=_BIDIRECTIONAL_STICKY_HOURS)

# Recorder queries are split into spans of at most this length, so a busy
# sensor (a grid meter updating every few seconds) does not load ten days of
# rows at once.
HISTORY_CHUNK = timedelta(days=1)

# Nimbus publishes an interval's record after the interval ends. Search this
# far past the last missing interval for its record.
NIMBUS_PUBLISH_LAG = timedelta(minutes=15)

# Used until the live coordinator has recorded its own read offset.
DEFAULT_SAMPLE_OFFSET_S = 150.0

# Skip reasons reported in the summary.
REASON_NO_HISTORY = "no recorder history for the required inputs"
REASON_DAY_UNREADABLE = "published day file could not be read from GitHub"
REASON_NO_NIMBUS_RECORD = "no Nimbus record in recorder history"


def recorder_keep_days(hass: Any) -> int | None:
    """Return the recorder's ``purge_keep_days``, or None if unknown."""
    if "recorder" not in hass.config.components:
        return None
    try:
        from homeassistant.components.recorder import get_instance

        return int(get_instance(hass).keep_days)
    except Exception:  # pylint: disable=broad-except
        return None


async def async_fetch_history(
    hass: Any,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    attributes: bool = False,
) -> dict[str, list[Any]]:
    """Return every recorded state of ``entity_ids`` between start and end.

    One recorder query for all entities, run in the recorder's executor. The
    first state of each entity is its state at ``start`` when the recorder
    has one (``include_start_time_state``). Entities with no history map to
    an empty list or are absent.

    The query starts a microsecond early: the recorder compares strictly on
    both sides of ``start``, so a state written exactly on an interval
    boundary would otherwise be in neither the start state nor the changes.
    """
    from homeassistant.components.recorder import get_instance, history

    return await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start - timedelta(microseconds=1),
            end,
            entity_ids,
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=not attributes,
        )
    )


class HistoryTimeline:
    """Answer "what was entity X's state at time T" from recorder history."""

    def __init__(self, history: dict[str, list[Any]]) -> None:
        """Index the states of each entity by ``last_updated``."""
        self._times: dict[str, list[float]] = {}
        self._states: dict[str, list[Any]] = {}
        for entity_id, states in history.items():
            ordered = sorted(
                (s for s in states if hasattr(s, "last_updated")),
                key=lambda s: s.last_updated,
            )
            self._times[entity_id] = [s.last_updated.timestamp() for s in ordered]
            self._states[entity_id] = ordered

    def state_at(self, entity_id: str, when: datetime) -> Any:
        """Return the latest state at or before ``when``, or None."""
        times = self._times.get(entity_id)
        if not times:
            return None
        idx = bisect.bisect_right(times, when.timestamp())
        if idx == 0:
            return None
        return self._states[entity_id][idx - 1]


class HistoricalRecordBuilder(RecordBuilder):
    """A record builder that reads states from recorder history.

    Starts with fresh cross-interval state; call ``build_at`` (or
    ``_missing_required_inputs``) in time order so EV inference sees the
    historical sequence.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Create a builder with the coordinator's effective configuration."""
        self._init_builder(config)
        # The live coordinator logs this once; a replay would repeat it.
        self._flex_derived_logged = True
        self.timeline = HistoryTimeline({})
        self.when: datetime = datetime.now(tz=UTC)

    def _get_state(self, entity_id: str) -> Any:
        """Return the entity's recorded state at ``self.when``."""
        return self.timeline.state_at(entity_id, self.when)

    def no_history_for(self, entity_ids: list[str]) -> bool:
        """True when none of ``entity_ids`` has any recorded state yet."""
        return all(self._get_state(e) is None for e in entity_ids if e)


@dataclass
class RebuildSummary:
    """What a rebuild checked, found and did. Returned by the service."""

    household_id: str
    source: str
    dry_run: bool
    window_start: str | None = None
    window_end: str | None = None
    intervals_checked: int = 0
    already_present: int = 0
    rebuilt_intervals: list[str] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    pushed: int = 0
    error: str | None = None
    # True when every missing interval was attempted and nothing needs a
    # retry (no GitHub read or push failure). Gates the per-version marker.
    complete: bool = False

    def skip(self, interval: str, reason: str) -> None:
        """Count one interval that could not be rebuilt."""
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1
        _LOGGER.debug("Rebuild: interval %s skipped: %s", interval, reason)

    def as_dict(self) -> dict[str, Any]:
        """Return the service response / log representation."""
        return {
            "household_id": self.household_id,
            "source": self.source,
            "dry_run": self.dry_run,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "intervals_checked": self.intervals_checked,
            "already_present": self.already_present,
            "rebuilt": len(self.rebuilt_intervals),
            "rebuilt_intervals": list(self.rebuilt_intervals),
            "skipped": sum(self.skipped_reasons.values()),
            "skipped_reasons": dict(self.skipped_reasons),
            "pushed": self.pushed,
            "error": self.error,
        }


def _ceil_interval(when: datetime) -> datetime:
    """Round up to the next 5-minute interval boundary (no-op if aligned)."""
    floor = interval_start_for(when)
    return floor if floor == when else floor + INTERVAL


def backfill_window(
    now: datetime,
    *,
    max_days: float,
    keep_days: float | None,
    not_before: list[datetime | None],
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the [start, end) window of interval starts to check.

    - The end is never later than the start of the current interval: that
      interval is still the live builder's.
    - The default start is ``max_days`` back, or the recorder's retention if
      shorter. An explicit start may reach back further, but not past the
      recorder's retention (there is no history to rebuild from).
    - Nothing before any ``not_before`` time (the config entry's creation,
      consent, the household's first published record).
    """
    current = interval_start_for(now.astimezone(UTC))
    retention_days = keep_days if keep_days else max_days
    retention = now - timedelta(days=retention_days)
    if start is None:
        start = now - timedelta(days=min(max_days, retention_days))
    start = max([start, retention, *(t for t in not_before if t is not None)])
    window_end = interval_start_for(min(end, current) if end else current)
    return _ceil_interval(start.astimezone(UTC)), window_end


def interval_starts(start: datetime, end: datetime) -> list[datetime]:
    """Return every interval start in [start, end)."""
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += INTERVAL
    return out


def replay_runs(
    candidates: list[datetime], warmup: timedelta = REPLAY_WARMUP
) -> list[tuple[datetime, datetime]]:
    """Group missing intervals into replay runs [replay_start, last_interval].

    Each run starts ``warmup`` before its first missing interval. Runs whose
    warm-up would overlap the previous run are merged, so the replay passes
    through the published intervals between them in order.
    """
    runs: list[tuple[datetime, datetime]] = []
    for t in sorted(candidates):
        if runs and t - warmup <= runs[-1][1] + INTERVAL:
            runs[-1] = (runs[-1][0], t)
        else:
            runs.append((t - warmup, t))
    return runs


def chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into HISTORY_CHUNK spans aligned to intervals."""
    out: list[tuple[datetime, datetime]] = []
    t = start
    while t < end:
        out.append((t, min(t + HISTORY_CHUNK, end)))
        t += HISTORY_CHUNK
    return out

