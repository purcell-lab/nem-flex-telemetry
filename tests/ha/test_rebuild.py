"""Rebuild missed intervals from recorder history, and the per-version backfill."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry import coordinator as coord_mod
from custom_components.nem_flex_telemetry.const import (
    CONF_CONSENT_TIMESTAMP,
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_SOURCE,
    CONF_TOKEN,
    DOMAIN,
    NIMBUS_TELEMETRY_ENTITY,
    SERVICE_REBUILD_INTERVALS,
    SOURCE_NIMBUS,
    VERSION,
)
from custom_components.nem_flex_telemetry.coordinator import NemFlexTelemetryCoordinator
from custom_components.nem_flex_telemetry.github_client import NemFlexGitHubClient
from custom_components.nem_flex_telemetry.rebuild import (
    REASON_NO_HISTORY,
    RebuildSummary,
    backfill_window,
    replay_runs,
)

from .test_nimbus_source import nimbus_record
from .test_persistence import _Resp
from .test_startup_readiness import REQUIRED

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "telemetry.schema.json").read_text()
)
HOUSEHOLD = "00000000-0000-4000-8000-000000000000"
NOW = datetime(2026, 10, 12, 5, 0, 30, tzinfo=UTC)
# Sat 10 Oct 2026 07:15 AEST, a $23/kWh interval lost to the old $20 cap.
SPIKE = datetime(2026, 10, 9, 21, 15, tzinfo=UTC)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _coordinator(
    hass: HomeAssistant, *, created: datetime | None = None, **extra
) -> NemFlexTelemetryCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_HOUSEHOLD_ID: HOUSEHOLD,
            CONF_REGION: "NSW1",
            CONF_POSTCODE_PREFIX: "200",
            CONF_TOKEN: "t",
            **REQUIRED,
            **extra,
        },
    )
    object.__setattr__(entry, "created_at", created or datetime(2026, 10, 1, tzinfo=UTC))
    entry.add_to_hass(hass)
    hass.config.components.add("recorder")
    return NemFlexTelemetryCoordinator(hass, entry)


def _client(published: dict[date, set[str]], first: date | None = date(2026, 10, 1)):
    client = MagicMock()
    client.first_published_date = AsyncMock(return_value=first)
    client.read_published_intervals = AsyncMock(
        side_effect=lambda _hh, days: {d: set(published.get(d, set())) for d in days}
    )
    client.append_records = AsyncMock()
    return client


def _history(series: dict[str, list[tuple[datetime, str]]], attributes=None):
    """A fake recorder: the same shape get_significant_states returns."""

    async def fetch(_hass, entity_ids, start, end, *, attributes_wanted=False, **kw):
        out: dict[str, list[State]] = {}
        for entity_id in entity_ids:
            points = series.get(entity_id, [])
            before = [p for p in points if p[0] <= start]
            during = [p for p in points if start < p[0] < end]
            states = ([before[-1]] if before else []) + during
            out[entity_id] = [
                State(
                    entity_id,
                    value,
                    (attributes or {}).get((entity_id, ts), {}),
                    last_changed=max(ts, start),
                    last_updated=max(ts, start),
                )
                for ts, value in states
            ]
        return out

    return fetch


def _steady(start: datetime, value: str = "1.0") -> dict[str, list[tuple[datetime, str]]]:
    return {entity_id: [(start, value)] for entity_id in REQUIRED.values()}


async def _rebuild(c, client, fetch, **kwargs) -> dict:
    with (
        patch.object(c, "_get_or_create_github_client", return_value=client),
        patch.object(coord_mod, "async_fetch_history", side_effect=fetch),
        patch.object(coord_mod, "recorder_keep_days", return_value=10),
    ):
        return await c.async_rebuild_intervals(**kwargs)


async def test_spike_interval_rebuilt_and_accepted(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The $23/kWh interval is rebuilt from history, validated and pushed."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    series = _steady(SPIKE - timedelta(hours=2))
    series["sensor.buy"] = [
        (SPIKE - timedelta(hours=2), "0.30"),
        (SPIKE + timedelta(seconds=40), "23.0"),
        (SPIKE + timedelta(minutes=5, seconds=40), "0.30"),
    ]
    published = {SPIKE.date(): {_iso(SPIKE - timedelta(minutes=10)), _iso(SPIKE - timedelta(minutes=5))}}
    client = _client(published)

    summary = await _rebuild(
        c, client, _history(series),
        start=SPIKE - timedelta(minutes=10), end=SPIKE + timedelta(minutes=5),
    )

    assert summary["intervals_checked"] == 3
    assert summary["already_present"] == 2
    assert summary["rebuilt_intervals"] == [_iso(SPIKE)]
    assert summary["pushed"] == 1
    client.append_records.assert_awaited_once()
    records = client.append_records.await_args.args[1]
    assert [r["interval_start_utc"] for r in records] == [_iso(SPIKE)]
    assert records[0]["price_signal_seen"] == 23.0
    # Same shape as a live record: the published schema accepts it.
    jsonschema.validate(records[0], SCHEMA)
    assert set(records[0]) == set(c._build_record(SPIKE))


async def test_history_is_read_at_the_live_sampling_offset(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """States are read where the live timer would have read them."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    c._sample_offset_s = 10.0  # live reads 10 s into each interval
    series = _steady(SPIKE - timedelta(hours=2))
    series["sensor.buy"] = [
        (SPIKE - timedelta(hours=2), "0.30"),
        (SPIKE + timedelta(seconds=40), "23.0"),
    ]
    client = _client({})
    await _rebuild(c, client, _history(series), start=SPIKE, end=SPIKE + timedelta(minutes=5))
    records = client.append_records.await_args.args[1]
    assert records[0]["price_signal_seen"] == 0.30


async def test_published_and_buffered_intervals_are_skipped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Nothing is rebuilt when GitHub or the buffer already has the interval."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    c._buffer.append({"interval_start_utc": _iso(SPIKE)})
    client = _client({SPIKE.date(): {_iso(SPIKE - timedelta(minutes=5))}})
    fetch = AsyncMock()
    summary = await _rebuild(
        c, client, fetch, start=SPIKE - timedelta(minutes=5), end=SPIKE + timedelta(minutes=5)
    )
    assert summary["already_present"] == 2
    assert summary["rebuilt"] == 0
    fetch.assert_not_awaited()
    client.append_records.assert_not_awaited()


async def test_dry_run_pushes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A dry run reports the intervals it would rebuild and pushes none."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    client = _client({})
    summary = await _rebuild(
        c, client, _history(_steady(SPIKE - timedelta(hours=2))),
        start=SPIKE, end=SPIKE + timedelta(minutes=10), dry_run=True,
    )
    assert summary["dry_run"] is True
    assert summary["rebuilt_intervals"] == [_iso(SPIKE), _iso(SPIKE + timedelta(minutes=5))]
    assert summary["pushed"] == 0
    client.append_records.assert_not_awaited()


async def test_intervals_without_history_are_skipped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Before the earliest history, or with an unavailable input, nothing is built."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    series = _steady(SPIKE + timedelta(minutes=5))
    series["sensor.load"] = [
        (SPIKE + timedelta(minutes=5), "1.0"),
        (SPIKE + timedelta(minutes=10), "unavailable"),
    ]
    client = _client({})
    summary = await _rebuild(
        c, client, _history(series), start=SPIKE, end=SPIKE + timedelta(minutes=15)
    )
    assert summary["rebuilt_intervals"] == [_iso(SPIKE + timedelta(minutes=5))]
    assert summary["skipped_reasons"] == {
        REASON_NO_HISTORY: 1,
        "required input(s) unavailable: entity_total_load_kw=sensor.load": 1,
    }


async def test_ev_state_is_replayed_in_time_order(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """SOC-delta inference sees the replayed previous interval."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    t0 = SPIKE - timedelta(hours=2)
    series = _steady(t0)
    series["sensor.ev1_power_balance_shadow_price"] = [(t0, "0.1")]
    # SOC rises 2 % per interval, setpoint not published: only the delta
    # between consecutive intervals says "charging".
    series["sensor.ev1_state_of_charge"] = [
        (SPIKE + timedelta(minutes=5 * k - 30), str(40 + 2 * k)) for k in range(12)
    ]
    client = _client({})
    await _rebuild(c, client, _history(series), start=SPIKE, end=SPIKE + timedelta(minutes=5))
    ev1 = next(a for a in client.append_records.await_args.args[1][0]["assets"] if a["asset_id"] == "ev1")
    assert ev1["connection_state"] == "charging"


async def test_default_window_starts_at_first_published_record(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The automatic window does not reach before the household existed."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    first = datetime(2026, 10, 11, 23, 0, tzinfo=UTC)
    client = _client({first.date(): {_iso(first)}}, first=first.date())
    summary = await _rebuild(c, client, _history(_steady(first)), dry_run=True)
    assert summary["window_start"] == "2026-10-11T00:00:00Z"
    assert summary["window_end"] == "2026-10-12T05:00:00Z"
    # 23:00 .. 04:55 = 72 intervals, the first already published.
    assert summary["intervals_checked"] == 72
    assert summary["already_present"] == 1
    assert summary["rebuilt"] == 71


async def test_household_that_never_published_is_not_backfilled(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """No published record: the default window is empty and complete."""
    freezer.move_to(NOW)
    c = _coordinator(hass)
    client = _client({}, first=None)
    with patch.object(c, "_get_or_create_github_client", return_value=client):
        summary = await c._async_rebuild_locked(None, None, dry_run=False)
    assert summary.complete
    client.read_published_intervals.assert_not_awaited()


def test_window_clamping() -> None:
    """Retention, entry creation and the current interval bound the window."""
    now = datetime(2026, 10, 12, 5, 3, 20, tzinfo=UTC)
    current = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    # Default: 10 days back, rounded up to an interval boundary.
    start, end = backfill_window(now, max_days=10, keep_days=10, not_before=[])
    assert (start, end) == (datetime(2026, 10, 2, 5, 5, tzinfo=UTC), current)
    # Recorder keeps 3 days.
    start, _ = backfill_window(now, max_days=10, keep_days=3, not_before=[])
    assert start == datetime(2026, 10, 9, 5, 5, tzinfo=UTC)
    # Entry created later than that.
    created = datetime(2026, 10, 10, 1, 2, tzinfo=UTC)
    start, _ = backfill_window(now, max_days=10, keep_days=3, not_before=[created, None])
    assert start == datetime(2026, 10, 10, 1, 5, tzinfo=UTC)
    # An explicit range reaches past max_days but not past retention, and
    # never into the current interval.
    start, end = backfill_window(
        now, max_days=10, keep_days=30, not_before=[],
        start=datetime(2026, 9, 1, tzinfo=UTC), end=now + timedelta(hours=1),
    )
    assert start == datetime(2026, 9, 12, 5, 5, tzinfo=UTC)
    assert end == current


async def test_window_never_before_consent(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Even an explicit start is clamped to consent and entry creation."""
    freezer.move_to(NOW)
    c = _coordinator(hass, **{CONF_CONSENT_TIMESTAMP: "2026-10-12T04:40:00+00:00"})
    summary = await _rebuild(
        c, _client({}), _history({}), start=datetime(2026, 10, 1, tzinfo=UTC), dry_run=True
    )
    assert summary["window_start"] == "2026-10-12T04:40:00Z"
    assert summary["intervals_checked"] == 4


def test_replay_runs_merge_close_gaps() -> None:
    """Missing intervals within the warm-up of each other replay together."""
    t = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
    runs = replay_runs([t, t + timedelta(minutes=30), t + timedelta(hours=5)])
    assert runs == [
        (t - timedelta(hours=1), t + timedelta(minutes=30)),
        (t + timedelta(hours=4), t + timedelta(hours=5)),
    ]


async def test_builder_reads_only_listed_entities(hass: HomeAssistant) -> None:
    """The history query covers every entity the live builder reads."""
    c = _coordinator(hass)
    read: list[str] = []
    original = c._get_state

    def spy(entity_id: str):
        read.append(entity_id)
        return original(entity_id)

    with patch.object(c, "_get_state", side_effect=spy):
        c._build_record()
        c._missing_required_inputs()
    assert set(read) <= set(c.entities_read())


async def test_nimbus_records_recovered_from_attribute_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Nimbus source: the missing interval's record comes from attributes.record."""
    freezer.move_to(NOW)
    c = _coordinator(hass, **{CONF_SOURCE: SOURCE_NIMBUS})
    missing = SPIKE
    kept = nimbus_record(_iso(missing))
    kept.update(region="NSW1", postcode_prefix="200", price_signal_seen=23.0)
    published_at = missing + timedelta(minutes=5, seconds=20)
    series = {NIMBUS_TELEMETRY_ENTITY: [(published_at, "ok")]}
    attrs = {(NIMBUS_TELEMETRY_ENTITY, published_at): {"record": kept}}
    client = _client({})
    summary = await _rebuild(
        c, client, _history(series, attrs),
        start=missing, end=missing + timedelta(minutes=10),
    )
    assert summary["rebuilt_intervals"] == [_iso(missing)]
    assert summary["skipped_reasons"] == {"no Nimbus record in recorder history": 1}
    assert client.append_records.await_args.args[1] == [kept]


async def test_backfill_runs_once_per_version(hass: HomeAssistant) -> None:
    """The automatic backfill runs on the first start of a version only."""
    c = _coordinator(hass)
    c._inputs_ready.set()
    done = RebuildSummary(HOUSEHOLD, "haeo", False, complete=True)
    with patch.object(c, "_async_rebuild_locked", AsyncMock(return_value=done)) as run:
        c.async_schedule_backfill()
        await hass.async_block_till_done()
        assert run.await_count == 1
        assert c._backfill_version == VERSION

        # Second start: the marker was persisted, nothing runs.
        c2 = NemFlexTelemetryCoordinator(hass, c.config_entry)
        await c2.async_load_state()
        c2._inputs_ready.set()
        c2.async_schedule_backfill()
        await hass.async_block_till_done()
        assert run.await_count == 1


async def test_incomplete_backfill_retries_next_start(hass: HomeAssistant) -> None:
    """A failed push leaves the marker unset, so the next start retries."""
    c = _coordinator(hass)
    c._backfill_version = "0.7.0"
    c._inputs_ready.set()
    failed = RebuildSummary(HOUSEHOLD, "haeo", False, error="push failed", complete=False)
    with patch.object(c, "_async_rebuild_locked", AsyncMock(return_value=failed)):
        c.async_schedule_backfill()
        await hass.async_block_till_done()
    assert c._backfill_version == "0.7.0"


async def test_service_returns_summary(hass: HomeAssistant) -> None:
    """The service forwards its fields and responds with each summary."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=3,
        data={CONF_HOUSEHOLD_ID: HOUSEHOLD, CONF_REGION: "NSW1",
              CONF_POSTCODE_PREFIX: "200", CONF_TOKEN: "t"},
    )
    entry.add_to_hass(hass)
    fake = MagicMock()
    fake.async_load_state = AsyncMock()
    fake.async_config_entry_first_refresh = AsyncMock()
    fake.async_handle_stop = AsyncMock()
    fake.async_shutdown = AsyncMock()
    fake.async_rebuild_intervals = AsyncMock(return_value={"rebuilt": 0})
    fake.household_id = HOUSEHOLD
    fake.region = "NSW1"
    with (
        patch("custom_components.nem_flex_telemetry.NemFlexTelemetryCoordinator", return_value=fake),
        patch("custom_components.nem_flex_telemetry.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        fake.async_schedule_backfill.assert_called_once()
        response = await hass.services.async_call(
            DOMAIN, SERVICE_REBUILD_INTERVALS,
            {"start": "2026-10-10 07:00:00", "dry_run": True},
            blocking=True, return_response=True,
        )
    assert response == {"households": {HOUSEHOLD: {"rebuilt": 0}}}
    kwargs = fake.async_rebuild_intervals.await_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["end"] is None
    assert kwargs["start"].tzinfo is not None


async def test_read_published_intervals() -> None:
    """Day files are read raw, once each; a missing file is an empty day."""
    calls: list[str] = []
    body = "\n".join(
        json.dumps({"interval_start_utc": t}) for t in ["2026-10-09T21:05:00Z", "2026-10-09T21:10:00Z"]
    ) + "\nnot json\n"

    class _Session:
        def get(self, url):
            calls.append(url)
            return _Resp(200, body) if url.endswith("/09.jsonl") else _Resp(404, "")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    client = NemFlexGitHubClient(token="t", repo_name="purcell-lab/nem-flex-telemetry")
    with (
        patch("custom_components.nem_flex_telemetry.github_client.aiohttp.ClientSession",
              return_value=_Session()),
        patch.object(client, "_check_rate_limit", AsyncMock()),
    ):
        result = await client.read_published_intervals(
            HOUSEHOLD, [date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 9)]
        )
    assert result == {
        date(2026, 10, 9): {"2026-10-09T21:05:00Z", "2026-10-09T21:10:00Z"},
        date(2026, 10, 10): set(),
    }
    assert len(calls) == 2
    assert calls[0].endswith(f"/contents/data/raw/{HOUSEHOLD}/2026/10/09.jsonl")

