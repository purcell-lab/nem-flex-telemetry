"""Clear handling when the GitHub account cannot write to the repo (#14)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nem_flex_telemetry.config_flow import NemFlexTelemetryConfigFlow
from custom_components.nem_flex_telemetry.const import (
    CONF_HOUSEHOLD_ID,
    CONF_POSTCODE_PREFIX,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.nem_flex_telemetry.coordinator import (
    ISSUE_NO_PUSH_ACCESS,
    NemFlexTelemetryCoordinator,
)
from custom_components.nem_flex_telemetry.github_client import (
    NemFlexGitHubClient,
    PushPermissionError,
)

GC = "custom_components.nem_flex_telemetry.github_client"


class _Resp:
    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, get: _Resp, put: _Resp | None = None) -> None:
        self._get, self._put = get, put
        self.puts = 0

    def get(self, url):
        return self._get

    def put(self, url, json=None):
        self.puts += 1
        return self._put

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client() -> NemFlexGitHubClient:
    return NemFlexGitHubClient(token="t", repo_name="purcell-lab/nem-flex-telemetry")


async def test_put_404_raises_permission_error_without_retry() -> None:
    """A 404 on PUT is a permission error and is not retried."""
    session = _Session(get=_Resp(404), put=_Resp(404, {"message": "Not Found"}))
    sleep = AsyncMock()
    with patch(f"{GC}.asyncio.sleep", sleep):
        with pytest.raises(PushPermissionError, match="does not have write access"):
            await _client()._push_with_retry(session, "data/raw/x/2026/10/08.jsonl", "{}\n", "m")
    assert session.puts == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"permissions": {"push": True}}, True),
        (200, {"permissions": {"push": False, "pull": True}}, False),
        (200, {}, None),
        (500, {}, None),
    ],
)
async def test_has_push_access(status, body, expected) -> None:
    """permissions.push maps to True/False; unknown maps to None."""
    with patch(f"{GC}.aiohttp.ClientSession", return_value=_Session(get=_Resp(status, body))):
        assert await _client().has_push_access() is expected


async def test_coordinator_raises_repair_once_and_clears(hass: HomeAssistant) -> None:
    """Permission failures keep records, raise one repair, and clear on success."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_HOUSEHOLD_ID: "00000000-0000-4000-8000-000000000000",
            CONF_REGION: "NSW1",
            CONF_POSTCODE_PREFIX: "200",
            CONF_TOKEN: "t",
        },
    )
    entry.add_to_hass(hass)
    c = NemFlexTelemetryCoordinator(hass, entry)
    client = MagicMock()
    client.append_records = AsyncMock(side_effect=PushPermissionError("no write access"))
    c._buffer.extend([{"interval_start_utc": "2026-10-08T00:00:00Z"}] * 3)
    registry = ir.async_get(hass)
    with patch.object(c, "_get_or_create_github_client", return_value=client):
        await c._async_push_buffer()
        await c._async_push_buffer()
        assert len(c._buffer) == 3
        assert registry.async_get_issue(DOMAIN, ISSUE_NO_PUSH_ACCESS) is not None

        client.append_records = AsyncMock(return_value=None)
        await c._async_push_buffer()
    assert len(c._buffer) == 0
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_PUSH_ACCESS) is None


async def _poll(hass: HomeAssistant, can_push) -> NemFlexTelemetryConfigFlow:
    flow = NemFlexTelemetryConfigFlow()
    flow.hass = hass
    flow._device_flow = {"device_code": "d", "interval": 5, "expires_in": 900}
    session = MagicMock()
    session.poll_for_token = AsyncMock(return_value="gho_x")
    with (
        patch("custom_components.nem_flex_telemetry.config_flow.DeviceFlowSession",
              return_value=session),
        patch("custom_components.nem_flex_telemetry.config_flow.fetch_authenticated_user",
              AsyncMock(return_value={"login": "tester"})),
        patch.object(NemFlexGitHubClient, "has_push_access", AsyncMock(return_value=can_push)),
    ):
        await flow._poll_for_token()
    return flow


@pytest.mark.parametrize(
    ("can_push", "next_step"),
    [(True, "identity"), (None, "identity"), (False, "no_push_access")],
)
async def test_setup_checks_write_access(hass: HomeAssistant, can_push, next_step) -> None:
    """Device Flow routes to a warning step only when access is known missing."""
    flow = await _poll(hass, can_push)
    assert flow._poll_next_step == next_step


async def test_no_push_access_step_continues_to_identity(hass: HomeAssistant) -> None:
    """The warning step can be acknowledged and setup continues."""
    flow = await _poll(hass, False)
    flow.context = {"source": "user"}
    result = await flow.async_step_no_push_access()
    assert result["step_id"] == "no_push_access"
    assert result["description_placeholders"]["github_login"] == "tester"
    result = await flow.async_step_no_push_access({})
    assert result["step_id"] == "identity"
