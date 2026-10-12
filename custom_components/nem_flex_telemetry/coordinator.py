"""DataUpdateCoordinator for NEM Flex Telemetry.

Responsibilities:
- Read HAEO entity states every 5 minutes
- Build and validate the schema v2.0 telemetry record (18 flat fields + assets[] + deferrable_loads[])
- Derive flex headroom from battery limits when HAEO does not expose them directly
- Infer per-EV connection state and power_flow_capability (not from any entity)
- Track the last_bidirectional_ev_id across intervals (sticky)
- Re-run global entity sweep on every coordinator startup
- Buffer records in memory
- Push the buffer to GitHub on the hour (every 12 records = 1 hour of data)
- Expose status attributes to sensor.py
- Trigger HA re-authentication when the stored OAuth token is rejected (401)
- Rebuild unpublished intervals from recorder history (service, and once
  per integration version in the background; see rebuild.py)

The record field logic lives in record_builder.py so live and history
rebuilds share it.

All prices are stored in $/kWh (no /1000 conversion from v2.0 onwards).
All GitHub I/O is async (aiohttp via NemFlexGitHubClient).
Version: 0.3.0 / Schema: 2.0
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_MAX_DAYS,
    BACKFILL_READY_TIMEOUT_SECONDS,
    CONF_CONSENT_TIMESTAMP,
    CONF_GITHUB_LOGIN,
    CONF_HOUSEHOLD_ID,
    CONF_SOURCE,
    CONF_TOKEN,
    DEFAULT_BATTERY_MAX_CHARGE_KW,
    DEFAULT_BATTERY_MAX_DISCHARGE_KW,
    DEFAULT_DCEV_AC_TO_DC_KW,
    DEFAULT_DCEV_DC_TO_AC_KW,
    DEFAULT_INVERTER_AC_TO_DC_KW,
    DEFAULT_INVERTER_DC_TO_AC_KW,
    DOMAIN,
    ENTITY_BATTERY_MAX_CHARGE,
    ENTITY_BATTERY_MAX_DISCHARGE,
    ENTITY_DCEV_AC_TO_DC,
    ENTITY_DCEV_DC_TO_AC,
    ENTITY_INVERTER_AC_TO_DC,
    ENTITY_INVERTER_DC_TO_AC,
    GITHUB_REPO,
    MARKET_PRICE_MAX,
    MARKET_PRICE_MIN,
    NIMBUS_TELEMETRY_ENTITY,
    RECORDS_PER_PUSH,
    REQUIRED_ENTITY_FIELDS,
    SCHEMA_VERSION,
    SHADOW_PRICE_MAX,
    SHADOW_PRICE_MIN,
    SOURCE_HAEO,
    SOURCE_NIMBUS,
    UPDATE_INTERVAL_SECONDS,
    VERSION,
)
from .discovery import discover_context_entities, run_global_sweep
from .github_client import (
    GitHubPushError,
    NemFlexGitHubClient,
    PushPermissionError,
    TokenInvalidError,
)
from .nimbus_source import read_nimbus_record
from .rebuild import (
    DEFAULT_SAMPLE_OFFSET_S,
    INTERVAL,
    NIMBUS_PUBLISH_LAG,
    REASON_DAY_UNREADABLE,
    REASON_NO_HISTORY,
    REASON_NO_NIMBUS_RECORD,
    HistoricalRecordBuilder,
    HistoryTimeline,
    RebuildSummary,
    async_fetch_history,
    backfill_window,
    chunks,
    interval_starts,
    recorder_keep_days,
    replay_runs,
)
from .record_builder import RecordBuilder, format_interval, interval_start_for

ISSUE_NO_PUSH_ACCESS = "no_push_access"

_LOGGER = logging.getLogger(__name__)

# Persistent state (#18, #19): buffer and push stats survive HA restarts.
STORAGE_VERSION = 1
BUFFER_MAX_RECORDS = 288                 # 24 hours of 5-minute records
COHORT_REFRESH = timedelta(hours=6)
STOP_PUSH_TIMEOUT_S = 10.0               # best-effort push on HA stop

# Startup readiness (#21). HAEO and inverter integrations can take a few
# minutes to publish states after a restart. During this grace period
# fallbacks and missing inputs are logged at DEBUG only.
STARTUP_GRACE_INTERVALS = 3


class CoordinatorData:
    """Data class holding coordinator output for sensor consumption."""

    def __init__(self) -> None:
        """Initialise with default values."""
        self.last_push_time: datetime | None = None
        self.records_pushed_today: int = 0
        self.push_errors: int = 0
        self.cohort_size: int = 0
        self.buffer_size: int = 0
        self.skipped_intervals: int = 0
        self.validation_errors: int = 0
        self.source: str = SOURCE_HAEO
        self.source_status: str | None = None
        self.unmapped_entities: list[str] = []


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Return the effective configuration for an entry.

    Setup values live in ``entry.data``; later edits from the options flow
    live in ``entry.options`` and take precedence (#16).
    """
    return {**entry.data, **entry.options}


class NemFlexTelemetryCoordinator(RecordBuilder, DataUpdateCoordinator[CoordinatorData]):
    """Coordinate 5-minute telemetry reads and hourly GitHub pushes.

    The record field logic lives in ``RecordBuilder``; the coordinator feeds
    it live states from ``hass.states``.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.config_entry = entry
        # Configuration, region, postcode prefix, asset capacities and the
        # cross-interval EV inference state (record_builder.RecordBuilder).
        self._init_builder(entry_config(entry))

        self.household_id: str = self._config[CONF_HOUSEHOLD_ID]
        self._github_login: str = self._config.get(CONF_GITHUB_LOGIN, "")

        # Record source (#27): build from mapped entities, or relay Nimbus.
        self.source: str = self._config.get(CONF_SOURCE) or SOURCE_HAEO
        self._nimbus_unsub: CALLBACK_TYPE | None = None

        # Record buffer (max 24 hours = 288 records). Persisted to
        # .storage/nem_flex_telemetry.<entry_id> so a restart does not lose
        # records waiting for the hourly push (#18).
        self._buffer: deque[dict[str, Any]] = deque(maxlen=BUFFER_MAX_RECORDS)
        self._data = CoordinatorData()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

        # Lazy-initialised GitHub client
        self._github_client: NemFlexGitHubClient | None = None
        self._push_error_count: int = 0
        self._no_push_access: bool = False
        self._records_pushed_today: int = 0
        # Local (HA timezone) date the daily counter belongs to (#19).
        self._push_day: str | None = None
        self._cohort_checked: datetime | None = None

        # Power-rating health-check (#21): last known live/fallback status per
        # entity, so changes are logged and startup fallbacks are not reported
        # as permanent.
        self._power_rating_live: dict[str, bool] = {}
        self._power_ratings_logged: bool = False
        self._intervals_seen: int = 0
        self._skipped_intervals: int = 0
        self._missing_warned: frozenset[str] = frozenset()
        # Records rejected by _validate_record (#30): counted and persisted
        # separately from push errors; one WARNING per failing field.
        self._validation_error_count: int = 0
        self._invalid_warned: set[str] = set()

        # Context entities discovered at first update
        self._context_entities: dict[str, str | None] = {}
        self._context_discovered: bool = False

        # Global sweep: re-run on every coordinator startup
        self._last_sweep_unmapped: list[str] = []

        # History rebuild. Seconds into the interval at which the live
        # builder reads states (its timer is not aligned to the clock), so a
        # rebuilt interval reads history at the same moment. Persisted.
        self._sample_offset_s: float | None = None
        # Integration VERSION the automatic backfill last completed for.
        self._backfill_version: str | None = None
        # Set once a live interval has been built from ready inputs, so the
        # automatic backfill reuses the startup-readiness check (#21).
        self._inputs_ready = asyncio.Event()
        self._rebuild_lock = asyncio.Lock()

    def _get_state(self, entity_id: str) -> Any:
        """Live state lookup for the record builder."""
        return self.hass.states.get(entity_id)

    def _get_or_create_github_client(self) -> NemFlexGitHubClient:
        """Return the GitHub client, creating it if needed."""
        if self._github_client is None:
            self._github_client = NemFlexGitHubClient(
                token=self._config[CONF_TOKEN],
                repo_name=GITHUB_REPO,
            )
        return self._github_client

    def _log_power_rating_health_check(self) -> None:
        """Log which power-rating entities are live and which use fallbacks.

        Runs every interval but only logs changes. During the startup grace
        period a fallback is logged at DEBUG, because the entity may simply
        not have published yet (#21). After the grace period a remaining
        fallback is logged once as a WARNING, and a later recovery is logged
        at INFO.
        """
        checks = (
            ("battery max charge", ENTITY_BATTERY_MAX_CHARGE,
             DEFAULT_BATTERY_MAX_CHARGE_KW),
            ("battery max discharge", ENTITY_BATTERY_MAX_DISCHARGE,
             DEFAULT_BATTERY_MAX_DISCHARGE_KW),
            ("hybrid inverter AC->DC", ENTITY_INVERTER_AC_TO_DC,
             DEFAULT_INVERTER_AC_TO_DC_KW),
            ("hybrid inverter DC->AC", ENTITY_INVERTER_DC_TO_AC,
             DEFAULT_INVERTER_DC_TO_AC_KW),
            ("DCEV charger AC->DC", ENTITY_DCEV_AC_TO_DC,
             DEFAULT_DCEV_AC_TO_DC_KW),
            ("DCEV charger DC->AC", ENTITY_DCEV_DC_TO_AC,
             DEFAULT_DCEV_DC_TO_AC_KW),
        )
        in_grace = self._intervals_seen <= STARTUP_GRACE_INTERVALS
        live_count = 0
        for label, entity_id, default_kw in checks:
            value, is_live = self._resolve_number_kw(entity_id, default_kw)
            live_count += is_live
            previous = self._power_rating_live.get(entity_id)
            if is_live:
                if previous is not True:
                    _LOGGER.info(
                        "Power-rating health-check: %s -> %.1f kW (live from %s)",
                        label, value, entity_id,
                    )
                self._power_rating_live[entity_id] = True
            elif in_grace:
                _LOGGER.debug(
                    "Power-rating health-check: %s not ready yet (%s); "
                    "using %.1f kW until it publishes",
                    label, entity_id, value,
                )
            elif previous is not False:
                _LOGGER.warning(
                    "Power-rating health-check: %s -> %.1f kW "
                    "(FALLBACK; %s missing or unavailable)",
                    label, value, entity_id,
                )
                self._power_rating_live[entity_id] = False
        if not self._power_ratings_logged and (not in_grace or live_count == len(checks)):
            _LOGGER.info(
                "Power-rating health-check complete: %d live, %d fallback",
                live_count, len(checks) - live_count,
            )
            self._power_ratings_logged = True

    async def _async_run_global_sweep(self) -> None:
        """Run the global entity sweep and log unmapped entities."""
        unmapped = run_global_sweep(self.hass)
        self._last_sweep_unmapped = unmapped
        self._data.unmapped_entities = unmapped
        if unmapped:
            _LOGGER.info(
                "Global sweep found %d unmapped entit%s on startup: %s",
                len(unmapped),
                "y" if len(unmapped) == 1 else "ies",
                ", ".join(unmapped),
            )

    async def _async_discover_context(self) -> None:
        """Discover context entities at first update and log them."""
        self._context_entities = await discover_context_entities(
            self.hass, region=self.region
        )
        self._context_discovered = True
        _LOGGER.info(
            "Context entities: %s",
            {k: v for k, v in self._context_entities.items() if v is not None},
        )

    async def _async_update_data(self) -> CoordinatorData:
        """Poll HAEO entities, buffer the record, and push to GitHub when due.

        Called automatically every UPDATE_INTERVAL_SECONDS by the base class.
        Triggers re-authentication if the stored token is rejected by GitHub.
        """
        if self.source == SOURCE_NIMBUS:
            return await self._async_update_from_nimbus()

        # One-time context entity discovery
        if not self._context_discovered:
            await self._async_discover_context()
            await self._async_run_global_sweep()

        self._intervals_seen += 1

        # Power-rating health-check: logs live vs fallback status changes.
        self._log_power_rating_health_check()

        # Do not publish an interval built from unavailable inputs: the
        # readers fall back to 0.0, which looks like real data (#21).
        missing = self._missing_required_inputs()
        if missing:
            self._skipped_intervals += 1
            if self._intervals_seen <= STARTUP_GRACE_INTERVALS:
                _LOGGER.debug("Skipping interval; inputs not ready: %s", ", ".join(missing))
            elif frozenset(missing) != self._missing_warned:
                _LOGGER.warning(
                    "Skipping telemetry interval: required input(s) unavailable: %s. "
                    "Check the entity mapping in the integration options.",
                    ", ".join(missing),
                )
                # Only remember what was actually reported, so inputs missing
                # during the grace period still warn once it ends.
                self._missing_warned = frozenset(missing)
            self._data.skipped_intervals = self._skipped_intervals
            return self._data
        if self._missing_warned:
            _LOGGER.info("Required inputs available again; resuming telemetry")
            self._missing_warned = frozenset()

        now_utc = datetime.now(tz=UTC)
        record = self._build_record(now_utc)
        self._sample_offset_s = (now_utc - interval_start_for(now_utc)).total_seconds()
        self._inputs_ready.set()

        # Validate using voluptuous (synchronous; run in executor)
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, record
            )
        except vol.Invalid as exc:
            # Skip this interval and count it; do not fail the update, which
            # would mark every diagnostic sensor unavailable (#30).
            self._note_validation_error(exc, record)
            return await self._async_after_buffer()

        # A quick restart can rebuild an interval already restored from
        # storage; keep the first copy (#18).
        if any(
            r.get("interval_start_utc") == validated["interval_start_utc"]
            for r in self._buffer
        ):
            _LOGGER.debug("Interval %s already buffered", validated["interval_start_utc"])
        else:
            self._buffer.append(validated)
        _LOGGER.debug(
            "Buffered record %s (buffer size: %d)",
            validated["interval_start_utc"],
            len(self._buffer),
        )
        return await self._async_after_buffer()

    async def _async_after_buffer(self) -> CoordinatorData:
        """Push when due and refresh the sensor-facing counters."""
        self._data.source = self.source
        self._roll_push_day()

        # Push when buffer reaches RECORDS_PER_PUSH
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()

        await self._async_refresh_cohort_size()
        self._sync_data()
        await self._async_save_state()
        return self._data

    # ------------------------------------------------------------------
    # Persistent state (#18, #19)
    # ------------------------------------------------------------------

    def _roll_push_day(self) -> None:
        """Reset the daily counter at local midnight (HA time zone)."""
        today = dt_util.now().date().isoformat()
        if self._push_day != today:
            if self._push_day is not None:
                self._records_pushed_today = 0
            self._push_day = today

    def _note_validation_error(self, exc: vol.Invalid, record: dict[str, Any]) -> None:
        """Count a rejected record; warn once per failing field (#30)."""
        self._validation_error_count += 1
        field = ".".join(str(p) for p in exc.path) or "record"
        value = record
        for part in exc.path:
            try:
                value = value[part]
            except (KeyError, IndexError, TypeError):
                value = None
                break
        if field not in self._invalid_warned:
            self._invalid_warned.add(field)
            _LOGGER.warning(
                "Skipping telemetry interval %s: %s (value %r). Further failures "
                "on this field are counted in the validation_errors attribute.",
                record.get("interval_start_utc"),
                exc,
                value,
            )
        _LOGGER.debug("Rejected record: %s", record)

    def _sync_data(self) -> None:
        """Copy internal counters into the sensor-facing data object."""
        self._data.buffer_size = len(self._buffer)
        self._data.records_pushed_today = self._records_pushed_today
        self._data.push_errors = self._push_error_count
        self._data.validation_errors = self._validation_error_count

    async def async_load_state(self) -> None:
        """Restore the buffer and push stats saved before the last restart."""
        try:
            stored = await self._store.async_load()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Could not load stored telemetry state: %s", exc)
            return
        if not stored:
            return

        records = stored.get("buffer") or []
        for record in records[-BUFFER_MAX_RECORDS:]:
            self._buffer.append(record)

        stats = stored.get("stats") or {}
        self._records_pushed_today = int(stats.get("records_pushed_today", 0))
        self._push_day = stats.get("push_day")
        self._push_error_count = int(stats.get("push_errors", 0))
        self._validation_error_count = int(stats.get("validation_errors", 0))
        self._data.cohort_size = int(stats.get("cohort_size", 0))
        if stats.get("last_push_time"):
            self._data.last_push_time = dt_util.parse_datetime(stats["last_push_time"])
        if stats.get("cohort_checked"):
            self._cohort_checked = dt_util.parse_datetime(stats["cohort_checked"])
        if stats.get("sample_offset_s") is not None:
            self._sample_offset_s = float(stats["sample_offset_s"])
        self._backfill_version = stored.get("backfill_version")
        self._roll_push_day()
        self._sync_data()

        if records:
            _LOGGER.info(
                "Restored %d buffered record(s) for household %s from storage",
                len(self._buffer),
                self.household_id,
            )

    async def _async_save_state(self) -> None:
        """Persist the buffer and push stats."""
        last_push = self._data.last_push_time
        payload = {
            "buffer": list(self._buffer),
            "stats": {
                "records_pushed_today": self._records_pushed_today,
                "push_day": self._push_day,
                "push_errors": self._push_error_count,
                "validation_errors": self._validation_error_count,
                "cohort_size": self._data.cohort_size,
                "last_push_time": last_push.isoformat() if last_push else None,
                "cohort_checked": (
                    self._cohort_checked.isoformat() if self._cohort_checked else None
                ),
                "sample_offset_s": self._sample_offset_s,
            },
            "backfill_version": self._backfill_version,
        }
        try:
            await self._store.async_save(payload)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("Could not save telemetry state: %s", exc)

    async def _async_refresh_cohort_size(self) -> None:
        """Refresh cohort size from the published status.json every 6 hours."""
        now = datetime.now(tz=UTC)
        if self._cohort_checked and now - self._cohort_checked < COHORT_REFRESH:
            return
        self._cohort_checked = now
        try:
            size = await self._get_or_create_github_client().get_cohort_size()
        except TokenInvalidError as exc:
            _LOGGER.warning(
                "Token invalid while fetching cohort size: %s. Triggering reauth.", exc
            )
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not update cohort size: %s", exc)
            return
        if size is not None:
            self._data.cohort_size = size

    async def async_handle_stop(self, _event: Event | None = None) -> None:
        """Save state on Home Assistant stop, then try one final push.

        Home Assistant does not unload config entries on stop, so
        ``async_shutdown`` alone never ran on a restart (#18). The save comes
        first so nothing is lost if the push times out.
        """
        await self._async_save_state()
        try:
            async with asyncio.timeout(STOP_PUSH_TIMEOUT_S):
                await self._async_push_buffer()
        except TimeoutError:
            _LOGGER.info("Final push timed out; %d record(s) kept for next start",
                         len(self._buffer))
        await self._async_save_state()

    # ------------------------------------------------------------------
    # Nimbus source (#27, nimbus#1634)
    # ------------------------------------------------------------------

    @callback
    def async_start_nimbus_listener(self) -> bool:
        """Relay each new Nimbus record as soon as Nimbus publishes it.

        Nimbus changes the sensor state once per completed interval. The
        5-minute poll in ``_async_update_from_nimbus`` is a backstop for a
        missed event; duplicates are dropped on ``interval_start_utc``.
        """
        if self.source != SOURCE_NIMBUS:
            return False

        async def _ingest_and_notify() -> None:
            if await self._async_ingest_nimbus():
                self._data.records_pushed_today = self._records_pushed_today
                self._data.push_errors = self._push_error_count
            self.async_update_listeners()

        @callback
        def _on_change(event: Event) -> None:
            self.hass.async_create_task(_ingest_and_notify())

        self._nimbus_unsub = async_track_state_change_event(
            self.hass, [NIMBUS_TELEMETRY_ENTITY], _on_change
        )
        return True

    @callback
    def async_stop_nimbus_listener(self) -> None:
        """Stop relaying Nimbus records (idempotent)."""
        if self._nimbus_unsub is not None:
            self._nimbus_unsub()
            self._nimbus_unsub = None

    async def _async_ingest_nimbus(self) -> bool:
        """Buffer the current Nimbus record if it is new and valid."""
        result = read_nimbus_record(
            self.hass.states.get(NIMBUS_TELEMETRY_ENTITY),
            region=self.region,
            postcode_prefix=self._postcode_prefix,
        )
        if result.record is None:
            if result.reason != self._data.source_status:
                _LOGGER.warning("Nimbus source: %s", result.reason)
            self._data.source_status = result.reason
            return False

        interval = result.record["interval_start_utc"]
        if any(r.get("interval_start_utc") == interval for r in self._buffer):
            return False
        try:
            validated = await self.hass.async_add_executor_job(
                self._validate_record, result.record
            )
        except vol.Invalid as exc:
            self._note_validation_error(exc, result.record)
            self._data.source_status = f"Nimbus record for {interval} failed validation: {exc}"
            self._data.validation_errors = self._validation_error_count
            return False

        if self._data.source_status:
            _LOGGER.info("Nimbus source: records available again")
        self._data.source_status = None
        self._inputs_ready.set()
        self._buffer.append(validated)
        _LOGGER.debug("Buffered Nimbus record %s (buffer size: %d)", interval, len(self._buffer))
        if len(self._buffer) >= RECORDS_PER_PUSH:
            await self._async_push_buffer()
        self._data.buffer_size = len(self._buffer)
        return True

    async def _async_update_from_nimbus(self) -> CoordinatorData:
        """5-minute backstop for the Nimbus source, then the usual push."""
        await self._async_ingest_nimbus()
        return await self._async_after_buffer()

    # ------------------------------------------------------------------
    # History rebuild: recover missed intervals from the recorder
    # ------------------------------------------------------------------

    @callback
    def async_schedule_backfill(self) -> None:
        """Run the automatic backfill once per integration VERSION.

        Runs in the background so setup is not delayed. The marker is saved
        only when the backfill completed, so a failed GitHub read or push is
        retried on the next start.
        """
        if self._backfill_version == VERSION:
            return
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_backfill_when_ready(),
            f"{DOMAIN} backfill {self.household_id}",
        )

    async def _async_backfill_when_ready(self) -> None:
        """Wait for Home Assistant and the inputs, then backfill."""
        started = asyncio.Event()

        @callback
        def _on_started(_hass: HomeAssistant) -> None:
            started.set()

        unsub = async_at_started(self.hass, _on_started)
        try:
            await started.wait()
        finally:
            unsub()
        # Same readiness signal as the live builder (#21): wait for one
        # interval built from ready inputs, but not forever, since the
        # rebuild itself reads history, not live states.
        try:
            async with asyncio.timeout(BACKFILL_READY_TIMEOUT_SECONDS):
                await self._inputs_ready.wait()
        except TimeoutError:
            _LOGGER.debug("Backfill: inputs not ready after %ss; running anyway",
                          BACKFILL_READY_TIMEOUT_SECONDS)

        try:
            summary = await self._async_rebuild_locked(None, None, dry_run=False)
        except Exception:  # pylint: disable=broad-except
            # A recorder or parsing failure must not surface as an unhandled
            # task error; the marker stays unset so the next start retries.
            _LOGGER.exception("Automatic backfill failed; will retry on next start")
            return
        if summary.complete:
            self._backfill_version = VERSION
            await self._async_save_state()

    async def async_rebuild_intervals(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Rebuild unpublished intervals in a window from recorder history.

        ``start`` and ``end`` default to the backfill window. Returns the
        summary the ``rebuild_intervals`` service responds with.
        """
        summary = await self._async_rebuild_locked(start, end, dry_run=dry_run)
        return summary.as_dict()

    async def _async_rebuild_locked(
        self, start: datetime | None, end: datetime | None, *, dry_run: bool
    ) -> RebuildSummary:
        """Serialise rebuilds (service call and automatic backfill)."""
        async with self._rebuild_lock:
            summary = RebuildSummary(self.household_id, self.source, dry_run)
            try:
                await self._async_rebuild(summary, start, end)
            except TokenInvalidError as exc:
                summary.error = str(exc)
                _LOGGER.warning("Rebuild: token invalid: %s. Triggering reauth.", exc)
                self.config_entry.async_start_reauth(self.hass)
            except GitHubPushError as exc:
                summary.error = str(exc)
            if summary.error:
                summary.complete = False
            _LOGGER.info(
                "Rebuild%s for household %s, %s to %s: %d checked, %d already "
                "published, %d rebuilt, %d pushed, %d skipped %s%s",
                " (dry run)" if dry_run else "",
                self.household_id,
                summary.window_start,
                summary.window_end,
                summary.intervals_checked,
                summary.already_present,
                len(summary.rebuilt_intervals),
                summary.pushed,
                sum(summary.skipped_reasons.values()),
                summary.skipped_reasons or "",
                f"; error: {summary.error}" if summary.error else "",
            )
            return summary

    def _rebuild_not_before(self) -> list[datetime | None]:
        """Never rebuild before this entry existed or consent was given."""
        times: list[datetime | None] = [getattr(self.config_entry, "created_at", None)]
        consent = self._config.get(CONF_CONSENT_TIMESTAMP)
        if consent:
            times.append(dt_util.parse_datetime(consent))
        return times

    async def _async_rebuild(
        self,
        summary: RebuildSummary,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        """Find the unpublished intervals in the window and rebuild them."""
        if "recorder" not in self.hass.config.components:
            summary.error = "the recorder integration is not loaded"
            return
        client = self._get_or_create_github_client()
        not_before = self._rebuild_not_before()

        # The default window never starts before the household's first
        # published record: before that it was not yet contributing.
        first_day = None
        if start is None:
            first_day = await client.first_published_date(self.household_id)
            if first_day is None:
                summary.complete = True
                _LOGGER.info("Rebuild: household %s has not published yet", self.household_id)
                return
            not_before.append(datetime.combine(first_day, datetime.min.time(), UTC))

        window_start, window_end = backfill_window(
            dt_util.utcnow(),
            max_days=BACKFILL_MAX_DAYS,
            keep_days=recorder_keep_days(self.hass),
            not_before=not_before,
            start=dt_util.as_utc(start) if start else None,
            end=dt_util.as_utc(end) if end else None,
        )
        summary.window_start = format_interval(window_start)
        summary.window_end = format_interval(window_end)
        intervals = interval_starts(window_start, window_end)
        if not intervals:
            summary.complete = True
            return

        published_by_day = await client.read_published_intervals(
            self.household_id, {t.date() for t in intervals}
        )
        if first_day is not None and published_by_day.get(first_day):
            first_record = min(published_by_day[first_day])
            intervals = [t for t in intervals if format_interval(t) >= first_record]
        summary.intervals_checked = len(intervals)

        buffered = {r.get("interval_start_utc") for r in self._buffer}
        candidates: list[datetime] = []
        for t in intervals:
            key = format_interval(t)
            published = published_by_day.get(t.date())
            if key in buffered or (published is not None and key in published):
                summary.already_present += 1
            elif published is None:
                summary.skip(key, REASON_DAY_UNREADABLE)
            else:
                candidates.append(t)

        if candidates:
            if self.source == SOURCE_NIMBUS:
                records = await self._async_rebuild_nimbus(summary, candidates)
            else:
                records = await self._async_rebuild_haeo(summary, candidates)
            await self._async_push_rebuilt(summary, records)

        summary.complete = (
            summary.error is None
            and REASON_DAY_UNREADABLE not in summary.skipped_reasons
        )

    def _validate_rebuilt(
        self, summary: RebuildSummary, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run rebuilt records through the live validation (executor job)."""
        valid: list[dict[str, Any]] = []
        for record in records:
            try:
                valid.append(self._validate_record(record))
            except vol.Invalid as exc:
                field = ".".join(str(p) for p in exc.path) or "record"
                summary.skip(
                    record.get("interval_start_utc", "?"), f"failed validation: {field}"
                )
        return valid

    async def _async_rebuild_haeo(
        self, summary: RebuildSummary, candidates: list[datetime]
    ) -> list[dict[str, Any]]:
        """Rebuild HAEO-source records by replaying recorder history."""
        wanted = set(candidates)
        offset = timedelta(
            seconds=self._sample_offset_s
            if self._sample_offset_s is not None
            else DEFAULT_SAMPLE_OFFSET_S
        )
        required = [self._config.get(f) for f in REQUIRED_ENTITY_FIELDS]
        records: list[dict[str, Any]] = []
        for replay_start, last in replay_runs(candidates):
            # Fresh inference state per run, replayed in time order.
            builder = HistoricalRecordBuilder(dict(self._config))
            entity_ids = builder.entities_read()
            for chunk_start, chunk_end in chunks(replay_start, last + INTERVAL):
                history = await async_fetch_history(
                    self.hass, entity_ids, chunk_start, chunk_end
                )
                builder.timeline = HistoryTimeline(history)
                for t in interval_starts(chunk_start, chunk_end):
                    builder.when = t + offset
                    key = format_interval(t)
                    # Same rule as live: no record from unavailable inputs.
                    if missing := builder._missing_required_inputs():
                        if t in wanted:
                            summary.skip(
                                key,
                                REASON_NO_HISTORY
                                if builder.no_history_for(required)
                                else "required input(s) unavailable: " + ", ".join(missing),
                            )
                        continue
                    record = builder._build_record(builder.when)
                    if t in wanted:
                        records.append(record)
                    # Otherwise replayed only to carry EV state forward.
        return await self.hass.async_add_executor_job(
            self._validate_rebuilt, summary, records
        )

    async def _async_rebuild_nimbus(
        self, summary: RebuildSummary, candidates: list[datetime]
    ) -> list[dict[str, Any]]:
        """Recover Nimbus records from the attribute history of its sensor."""
        wanted = {format_interval(t) for t in candidates}
        found: dict[str, dict[str, Any]] = {}
        search_end = min(candidates[-1] + INTERVAL + NIMBUS_PUBLISH_LAG, dt_util.utcnow())
        for chunk_start, chunk_end in chunks(candidates[0], search_end):
            if not any(
                chunk_start - INTERVAL - NIMBUS_PUBLISH_LAG <= t < chunk_end
                for t in candidates
            ):
                continue
            history = await async_fetch_history(
                self.hass, [NIMBUS_TELEMETRY_ENTITY], chunk_start, chunk_end, attributes=True
            )
            for state in history.get(NIMBUS_TELEMETRY_ENTITY, []):
                result = read_nimbus_record(
                    state, region=self.region, postcode_prefix=self._postcode_prefix
                )
                if result.record is None:
                    continue
                key = result.record["interval_start_utc"]
                if key in wanted and key not in found:
                    found[key] = result.record
        for key in sorted(wanted - found.keys()):
            summary.skip(key, REASON_NO_NIMBUS_RECORD)
        return await self.hass.async_add_executor_job(
            self._validate_rebuilt, summary, [found[k] for k in sorted(found)]
        )

    async def _async_push_rebuilt(
        self, summary: RebuildSummary, records: list[dict[str, Any]]
    ) -> None:
        """Push rebuilt records straight to GitHub (not via the buffer).

        A backfill can exceed the 288-record buffer. The client's dedupe on
        append still drops any interval that reached GitHub meanwhile.
        """
        records.sort(key=lambda r: r["interval_start_utc"])
        summary.rebuilt_intervals = [r["interval_start_utc"] for r in records]
        if summary.dry_run or not records:
            return
        try:
            await self._get_or_create_github_client().append_records(
                self.household_id,
                records,
                commit_message=(
                    f"telemetry: {self.household_id} rebuilt {len(records)} "
                    "interval(s) from history"
                ),
            )
        except GitHubPushError as exc:
            # Includes PushPermissionError. Nothing is buffered: the next
            # rebuild finds the same gaps and tries again.
            self._push_error_count += 1
            summary.error = f"push of rebuilt records failed: {exc}"
            self._sync_data()
            return
        summary.pushed = len(records)
        self._records_pushed_today += len(records)
        self._data.last_push_time = datetime.now(tz=UTC)
        self._sync_data()
        await self._async_save_state()
        self.async_update_listeners()

    def _validate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run lightweight voluptuous validation on the top-level record.

        Full JSON Schema validation (additionalProperties etc.) is run by the
        CI workflow via jsonschema. Here we just confirm critical fields are
        present and prices are in plausible $/kWh range.

        Market prices use the MARKET_PRICE_MIN to MARKET_PRICE_MAX $/kWh window. LP duals (shadow_*)
        are not market prices: they grow without a price-cap bound when a hard
        constraint binds, so they only get a wide sanity guard (#30).
        """
        _NEM_REGIONS = vol.In(["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
        _PRICE_RANGE = vol.All(
            vol.Coerce(float), vol.Range(min=MARKET_PRICE_MIN, max=MARKET_PRICE_MAX)
        )
        _SHADOW_RANGE = vol.All(
            vol.Coerce(float), vol.Range(min=SHADOW_PRICE_MIN, max=SHADOW_PRICE_MAX)
        )
        _KW_NON_NEG = vol.All(vol.Coerce(float), vol.Range(min=0))
        _ASSET = vol.Schema(
            {vol.Optional("shadow_power_balance_price"): vol.Any(None, _SHADOW_RANGE)},
            extra=vol.ALLOW_EXTRA,
        )

        schema = vol.Schema(
            {
                vol.Required("schema_version"): "2.0",
                vol.Required("interval_start_utc"): str,
                vol.Required("region"): _NEM_REGIONS,
                vol.Required("postcode_prefix"): vol.Match(r"^[0-9]{3}$"),
                vol.Required("net_import_kw"): vol.Coerce(float),
                vol.Required("solar_kw"): _KW_NON_NEG,
                vol.Required("house_load_kw"): _KW_NON_NEG,
                vol.Required("deferrable_load_kw"): _KW_NON_NEG,
                vol.Required("naive_baseline_kw"): vol.Coerce(float),
                vol.Required("naive_baseline_method"): vol.In(
                    ["subtraction", "haeo_counterfactual"]
                ),
                vol.Required("price_signal_seen"): _PRICE_RANGE,
                vol.Required("price_export_seen"): _PRICE_RANGE,
                vol.Required("envelope_import_limit_kw"): _KW_NON_NEG,
                vol.Required("envelope_export_limit_kw"): _KW_NON_NEG,
                vol.Required("flex_available_up_kw"): _KW_NON_NEG,
                vol.Required("flex_available_down_kw"): _KW_NON_NEG,
                vol.Optional("shadow_energy_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_load_forecast_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_solar_forecast_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_envelope_import_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Optional("shadow_envelope_export_price"): vol.Any(None, _SHADOW_RANGE),
                vol.Required("assets"): [_ASSET],
                vol.Required("deferrable_loads"): list,
            },
            extra=vol.ALLOW_EXTRA,
        )
        return schema(record)

    async def _async_push_buffer(self) -> None:
        """Flush the in-memory buffer to GitHub."""
        if not self._buffer:
            return

        records_to_push = list(self._buffer)
        self._buffer.clear()

        try:
            await self._get_or_create_github_client().append_records(
                self.household_id, records_to_push
            )
            count = len(records_to_push)
            self._records_pushed_today += count
            self._data.last_push_time = datetime.now(tz=UTC)
            if self._no_push_access:
                self._no_push_access = False
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_PUSH_ACCESS)
                _LOGGER.info("Write access to %s confirmed; pushes resumed", GITHUB_REPO)
            _LOGGER.info(
                "Pushed %d records for household %s (schema v%s, v%s)",
                count,
                self.household_id,
                SCHEMA_VERSION,
                VERSION,
            )

        except TokenInvalidError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            _LOGGER.error(
                "GitHub token invalid (total errors: %d): %s. Triggering re-authentication.",
                self._push_error_count,
                exc,
            )
            self.config_entry.async_start_reauth(self.hass)

        except PushPermissionError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            if not self._no_push_access:
                # Log and raise a repair once; keep buffering until access is
                # granted (#14). Up to 24 hours of records are kept.
                self._no_push_access = True
                _LOGGER.error("%s Records are kept and retried each hour.", exc)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_NO_PUSH_ACCESS,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_NO_PUSH_ACCESS,
                    translation_placeholders={"repo": GITHUB_REPO},
                    learn_more_url=(
                        "https://github.com/purcell-lab/nem-flex-telemetry/blob/main/"
                        "docs/INSTALL.md#push-fails-with-http-404"
                    ),
                )

        except GitHubPushError as exc:
            self._push_error_count += 1
            for r in reversed(records_to_push):
                self._buffer.appendleft(r)
            _LOGGER.error(
                "GitHub push failed (total errors: %d): %s",
                self._push_error_count,
                exc,
            )

    async def async_force_push(self) -> None:
        """Force an immediate push of the current buffer (for manual push service)."""
        _LOGGER.info("Force push triggered for household %s", self.household_id)
        await self._async_push_buffer()
        self._sync_data()
        await self._async_save_state()
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Attempt a final flush before unloading."""
        self.async_stop_nimbus_listener()
        _LOGGER.info(
            "Coordinator shutting down, attempting final push for %s",
            self.household_id,
        )
        await self._async_push_buffer()
        await self._async_save_state()
        await super().async_shutdown()
