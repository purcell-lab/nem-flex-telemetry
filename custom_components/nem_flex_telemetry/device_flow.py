"""GitHub OAuth Device Flow implementation for NEM Flex Telemetry.

Implements the GitHub Device Flow as specified at:
https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow

This module handles:
- Requesting a device code from GitHub
- Polling for the user access token while the user completes browser authorisation
- Fetching the authenticated user profile once a token is obtained

All network I/O is async (aiohttp). No blocking calls are made.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import (
    OAUTH_CLIENT_ID,
    OAUTH_DEVICE_CODE_URL,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    OAUTH_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# GitHub API base URL for authenticated user profile
_GITHUB_API_USER_URL = "https://api.github.com/user"

# Default poll interval when GitHub does not specify one (seconds)
_DEFAULT_POLL_INTERVAL = 5

# How many seconds to add to the interval on a slow_down response
_SLOW_DOWN_INCREMENT = 5


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class DeviceFlowError(Exception):
    """Base exception for Device Flow failures."""


class DeviceFlowExpired(DeviceFlowError):
    """The device code expired before the user authorised."""


class DeviceFlowDenied(DeviceFlowError):
    """The user explicitly denied authorisation on GitHub."""


class DeviceFlowInvalid(DeviceFlowError):
    """The device code is invalid or does not match."""


class DeviceFlowNetworkError(DeviceFlowError):
    """A network error occurred while communicating with GitHub."""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class DeviceFlowSession:
    """Orchestrates the GitHub OAuth Device Flow.

    Usage:
        session = DeviceFlowSession()
        code_data = await session.request_device_code()
        # Show code_data["user_code"] and code_data["verification_uri"] to user
        token = await session.poll_for_token(
            code_data["device_code"],
            code_data["interval"],
            code_data["expires_in"],
        )
        user = await fetch_authenticated_user(token)
    """

    def __init__(self) -> None:
        """Initialise the Device Flow session."""
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": OAUTH_USER_AGENT,
        }

    async def request_device_code(self) -> dict[str, Any]:
        """Request a device code from GitHub.

        POSTs to OAUTH_DEVICE_CODE_URL with client_id and scope.

        Returns:
            dict with keys: device_code, user_code, verification_uri,
            verification_uri_complete, expires_in, interval.

        Raises:
            DeviceFlowNetworkError: On any network or HTTP error.
        """
        payload = {
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPE,
        }
        _LOGGER.info("Requesting GitHub Device Flow code (scope: %s)", OAUTH_SCOPE)
        try:
            async with aiohttp.ClientSession(headers=self._headers) as http:
                async with http.post(OAUTH_DEVICE_CODE_URL, data=payload) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise DeviceFlowNetworkError(
                            f"GitHub returned HTTP {resp.status} on device code request: {text}"
                        )
                    data: dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise DeviceFlowNetworkError(
                f"Network error requesting device code: {exc}"
            ) from exc

        _LOGGER.info(
            "Device code obtained. User code: %s  Verify at: %s  Expires in: %ss",
            data.get("user_code"),
            data.get("verification_uri"),
            data.get("expires_in"),
        )
        return data

    async def poll_for_token(
        self,
        device_code: str,
        interval: int,
        expires_in: int,
    ) -> str:
        """Poll GitHub for the access token until the user authorises or the code expires.

        Respects GitHub's 'interval' guidance and handles slow_down by increasing
        the poll interval by 5 seconds. Never logs the device_code at info level.

        Args:
            device_code: The device_code from request_device_code().
            interval: Initial polling interval in seconds (from GitHub response).
            expires_in: Total seconds before the device code expires.

        Returns:
            The access token string on success.

        Raises:
            DeviceFlowExpired: The device code expired.
            DeviceFlowDenied: The user denied authorisation.
            DeviceFlowInvalid: The device code is invalid.
            DeviceFlowNetworkError: A network error occurred.
        """
        poll_interval = max(interval, _DEFAULT_POLL_INTERVAL)
        elapsed = 0
        payload = {
            "client_id": OAUTH_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }

        _LOGGER.info(
            "Beginning Device Flow token poll. Interval: %ss, expires in: %ss",
            poll_interval,
            expires_in,
        )

        while elapsed < expires_in:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            _LOGGER.debug(
                "Polling for token (elapsed: %ds / %ds)", elapsed, expires_in
            )

            try:
                async with aiohttp.ClientSession(headers=self._headers) as http:
                    async with http.post(OAUTH_TOKEN_URL, data=payload) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            _LOGGER.error(
                                "GitHub token poll returned HTTP %s: %s",
                                resp.status,
                                text,
                            )
                            raise DeviceFlowNetworkError(
                                f"GitHub returned HTTP {resp.status} during token poll: {text}"
                            )
                        data: dict[str, Any] = await resp.json()
            except aiohttp.ClientError as exc:
                raise DeviceFlowNetworkError(
                    f"Network error during token poll: {exc}"
                ) from exc

            # Successful token grant
            if "access_token" in data:
                _LOGGER.info("Device Flow authorisation successful.")
                return data["access_token"]

            error = data.get("error", "")

            if error == "authorization_pending":
                _LOGGER.debug("Device Flow: authorisation pending, continuing to poll.")
                continue

            if error == "slow_down":
                poll_interval += _SLOW_DOWN_INCREMENT
                _LOGGER.info(
                    "Device Flow: slow_down received, increasing interval to %ds.",
                    poll_interval,
                )
                elapsed -= poll_interval  # compensate so we do not overshoot expiry
                continue

            if error == "expired_token":
                _LOGGER.error("Device Flow: device code expired.")
                raise DeviceFlowExpired(
                    "The device code expired before the user authorised."
                )

            if error == "access_denied":
                _LOGGER.error("Device Flow: access denied by user.")
                raise DeviceFlowDenied("The user denied authorisation on GitHub.")

            if error == "incorrect_device_code":
                _LOGGER.error("Device Flow: incorrect device code.")
                raise DeviceFlowInvalid(
                    "The device code is invalid or does not match the client."
                )

            # Unknown error response
            _LOGGER.error("Device Flow: unknown error response: %s", data)
            raise DeviceFlowNetworkError(
                f"Unknown error from GitHub token endpoint: {data}"
            )

        # Loop exhausted without a token
        _LOGGER.error(
            "Device Flow: polling loop exhausted after %ds without a token.", elapsed
        )
        raise DeviceFlowExpired(
            "The device code polling window expired without authorisation."
        )


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


async def fetch_authenticated_user(token: str) -> dict[str, Any]:
    """Fetch the authenticated GitHub user profile for a given token.

    Calls https://api.github.com/user and returns the JSON response.
    Used after a successful Device Flow to retrieve the user's GitHub login,
    which is used to path-scope writes to data/raw/<login>/**.

    Args:
        token: A GitHub OAuth or personal access token.

    Returns:
        dict containing at minimum 'login' and 'id' keys.

    Raises:
        DeviceFlowNetworkError: On any network or HTTP error.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": OAUTH_USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Token is never logged at info level
    _LOGGER.info("Fetching authenticated GitHub user profile.")
    try:
        async with aiohttp.ClientSession(headers=headers) as http:
            async with http.get(_GITHUB_API_USER_URL) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise DeviceFlowNetworkError(
                        f"GitHub /user returned HTTP {resp.status}: {text}"
                    )
                user_data: dict[str, Any] = await resp.json()
    except aiohttp.ClientError as exc:
        raise DeviceFlowNetworkError(
            f"Network error fetching user profile: {exc}"
        ) from exc

    _LOGGER.info("Authenticated as GitHub user: %s", user_data.get("login"))
    return user_data
