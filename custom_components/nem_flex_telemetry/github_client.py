"""GitHub REST client for NEM Flex Telemetry.

Handles all interaction with the central GitHub repository using raw aiohttp
REST calls (no PyGithub dependency). This keeps the dependency footprint
minimal and all I/O async-native.

Operations:
- Append-or-create JSONL files at data/raw/<household_id>/YYYY/MM/DD.jsonl
- Retry logic with exponential backoff
- Rate-limit awareness (respects X-RateLimit-Remaining response headers)
- Token verification (defence against token swap after reauth)

The Authorization header accepts both GitHub OAuth tokens (issued via Device
Flow) and fine-grained personal access tokens. GitHub treats them identically
in the Authorization header.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import GITHUB_REPO, OAUTH_USER_AGENT

_LOGGER = logging.getLogger(__name__)

# GitHub REST API base
_GITHUB_API_BASE = "https://api.github.com"

# Max retries on transient errors
MAX_RETRIES = 3
# Initial backoff in seconds (doubles each retry)
BACKOFF_INITIAL = 5
# Rate-limit threshold: pause if fewer than this many requests remain
RATE_LIMIT_PAUSE_THRESHOLD = 10


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def dedupe_new_lines(existing_text: str, new_content: str) -> str:
    """Drop new JSONL lines whose interval_start_utc is already in the file.

    A retry after a push that reached GitHub but timed out locally, or a
    restored buffer (#18), would otherwise write the same interval twice.
    """
    seen: set[str] = set()
    for line in existing_text.splitlines():
        try:
            seen.add(json.loads(line)["interval_start_utc"])
        except (ValueError, KeyError, TypeError):
            continue
    kept: list[str] = []
    for line in new_content.splitlines():
        if not line.strip():
            continue
        try:
            key = json.loads(line)["interval_start_utc"]
        except (ValueError, KeyError, TypeError):
            kept.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return "".join(f"{line}\n" for line in kept)


class GitHubPushError(Exception):
    """Raised when a push to GitHub fails after all retries."""


class PushPermissionError(GitHubPushError):
    """The token is valid but cannot write to the telemetry repository.

    GitHub returns 404 (not 403) from the Contents API when the account has
    read access only, so a 404 on PUT means "no write access" (#14).
    Retrying cannot succeed.
    """


class TokenInvalidError(Exception):
    """Raised when the stored token is rejected by GitHub (HTTP 401).

    The coordinator catches this to trigger HA re-authentication via
    config_entry.async_start_reauth().
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NemFlexGitHubClient:
    """Manages JSONL file commits to the NEM Flex Telemetry central repo.

    All public methods are async and safe to call from the HA event loop.
    """

    def __init__(self, token: str, repo_name: str) -> None:
        """Initialise the GitHub client.

        Args:
            token: GitHub OAuth access token or fine-grained PAT with
                   contents:write. The Authorization header format is
                   identical for both token types.
            repo_name: Full repo name, e.g. "purcell-lab/nem-flex-telemetry".
        """
        self._token = token
        self._repo_name = repo_name
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": OAUTH_USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        """Build a full GitHub API URL."""
        return f"{_GITHUB_API_BASE}{path}"

    async def _check_rate_limit(self, session: aiohttp.ClientSession) -> None:
        """Check rate limit and sleep until reset if below threshold."""
        try:
            async with session.get(self._url("/rate_limit")) as resp:
                if resp.status != 200:
                    return
                data: dict[str, Any] = await resp.json()
            remaining = data.get("resources", {}).get("core", {}).get("remaining", 999)
            reset_ts = data.get("resources", {}).get("core", {}).get("reset", 0)
            if remaining < RATE_LIMIT_PAUSE_THRESHOLD:
                wait = max(0, reset_ts - datetime.now(tz=UTC).timestamp()) + 5
                _LOGGER.warning(
                    "GitHub rate limit low (%d remaining). Sleeping %.0fs until reset.",
                    remaining,
                    wait,
                )
                await asyncio.sleep(wait)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not check rate limit: %s", exc)

    async def verify_token(self, expected_login: str) -> None:
        """Verify that the stored token authenticates as expected_login.

        Calls GET /user and compares the returned login against the configured
        github_login. Raises TokenInvalidError if the token is rejected (401)
        or if the login does not match (defence against token swap).

        Args:
            expected_login: The github_login stored in the config entry.

        Raises:
            TokenInvalidError: Token is invalid or login mismatch.
            GitHubPushError: Network error during verification.
        """
        _LOGGER.debug("Verifying token against GitHub /user endpoint.")
        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with session.get(self._url("/user")) as resp:
                    if resp.status == 401:
                        raise TokenInvalidError(
                            "GitHub rejected the stored token (HTTP 401). Re-authentication required."
                        )
                    if resp.status != 200:
                        text = await resp.text()
                        raise GitHubPushError(
                            f"Unexpected HTTP {resp.status} from /user: {text}"
                        )
                    data: dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise GitHubPushError(f"Network error verifying token: {exc}") from exc

        actual_login = data.get("login", "")
        if actual_login.lower() != expected_login.lower():
            raise TokenInvalidError(
                f"Token login mismatch: expected '{expected_login}', got '{actual_login}'. "
                "Re-authentication required."
            )
        _LOGGER.debug("Token verified for GitHub user: %s", actual_login)

    async def has_push_access(self) -> bool | None:
        """Return whether the token can write to the telemetry repository.

        Reads ``permissions.push`` from ``GET /repos/{repo}``. Returns None if
        GitHub could not be reached, so callers can treat it as unknown.
        """
        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with session.get(self._url(f"/repos/{self._repo_name}")) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("Repo permission check returned HTTP %s", resp.status)
                        return None
                    data: dict[str, Any] = await resp.json()
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Repo permission check failed: %s", exc)
            return None
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            return None
        return bool(permissions.get("push"))

    async def append_records(
        self,
        household_id: str,
        records: list[dict[str, Any]],
        commit_message: str | None = None,
    ) -> None:
        """Append a list of telemetry records to the appropriate JSONL file(s).

        Records are grouped by UTC date. If a file already exists for a given
        date, the new records are appended. Deduplication is handled in the
        aggregation action.

        Args:
            household_id: Household slug used as the directory name.
            records: List of validated 12-field telemetry dicts.
            commit_message: Optional commit message override.

        Raises:
            TokenInvalidError: If the token is rejected (HTTP 401).
            GitHubPushError: If all retries are exhausted.
        """
        if not records:
            return

        # Group records by their UTC date (based on interval_start_utc)
        by_date: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            dt = datetime.fromisoformat(record["interval_start_utc"].replace("Z", "+00:00"))
            date_key = dt.strftime("%Y/%m/%d")
            by_date.setdefault(date_key, []).append(record)

        async with aiohttp.ClientSession(headers=self._headers) as session:
            await self._check_rate_limit(session)
            for date_key, date_records in by_date.items():
                year, month, day = date_key.split("/")
                file_path = f"data/raw/{household_id}/{year}/{month}/{day}.jsonl"
                new_content = (
                    "\n".join(json.dumps(r, separators=(",", ":")) for r in date_records)
                    + "\n"
                )
                msg = commit_message or (
                    f"telemetry: {household_id} {date_key} ({len(date_records)} records)"
                )
                await self._push_with_retry(session, file_path, new_content, msg)

    async def _push_with_retry(
        self,
        session: aiohttp.ClientSession,
        file_path: str,
        new_content: str,
        commit_message: str,
    ) -> None:
        """Append content to a file in the repo, creating it if it does not exist.

        Retries up to MAX_RETRIES times on transient GitHub errors.

        Raises:
            TokenInvalidError: On HTTP 401.
            GitHubPushError: After all retries are exhausted.
        """
        contents_url = self._url(f"/repos/{self._repo_name}/contents/{file_path}")
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Try to fetch the existing file to get its SHA and current content
                sha: str | None = None
                existing_content_b64: str | None = None

                async with session.get(contents_url) as get_resp:
                    if get_resp.status == 200:
                        get_data: dict[str, Any] = await get_resp.json()
                        sha = get_data.get("sha")
                        existing_content_b64 = get_data.get("content", "")
                    elif get_resp.status == 404:
                        # File does not exist yet; will be created
                        sha = None
                    elif get_resp.status == 401:
                        raise TokenInvalidError(
                            "GitHub rejected the stored token (HTTP 401). Re-authentication required."
                        )
                    else:
                        text = await get_resp.text()
                        raise GitHubPushError(
                            f"GitHub GET {file_path} returned HTTP {get_resp.status}: {text}"
                        )

                # Build the content to commit
                if existing_content_b64:
                    # Strip line breaks that GitHub adds to base64
                    cleaned = existing_content_b64.replace("\n", "")
                    existing_text = base64.b64decode(cleaned).decode("utf-8")
                    to_add = dedupe_new_lines(existing_text, new_content)
                    if not to_add:
                        _LOGGER.debug("All records already present in %s", file_path)
                        return
                    if existing_text and not existing_text.endswith("\n"):
                        existing_text += "\n"
                    combined = existing_text + to_add
                else:
                    combined = new_content

                payload: dict[str, Any] = {
                    "message": commit_message,
                    "content": base64.b64encode(combined.encode("utf-8")).decode("ascii"),
                }
                if sha:
                    payload["sha"] = sha

                async with session.put(contents_url, json=payload) as put_resp:
                    if put_resp.status in (200, 201):
                        action = "Updated" if sha else "Created"
                        _LOGGER.debug(
                            "%s %s (attempt %d)", action, file_path, attempt
                        )
                        return
                    if put_resp.status == 401:
                        raise TokenInvalidError(
                            "GitHub rejected the stored token (HTTP 401). Re-authentication required."
                        )
                    if put_resp.status in (403, 404):
                        raise PushPermissionError(
                            f"Your GitHub account does not have write access to "
                            f"{self._repo_name} (HTTP {put_resp.status} on {file_path}). "
                            "See https://github.com/purcell-lab/nem-flex-telemetry/blob/main/"
                            "docs/INSTALL.md#push-fails-with-http-404"
                        )
                    text = await put_resp.text()
                    raise GitHubPushError(
                        f"GitHub PUT {file_path} returned HTTP {put_resp.status}: {text}"
                    )

            except (TokenInvalidError, PushPermissionError):
                # Never retry a 401 or a permission error; bubble up immediately
                raise

            except Exception as exc:  # pylint: disable=broad-except
                last_exc = exc
                backoff = BACKOFF_INITIAL * (2 ** (attempt - 1))
                _LOGGER.warning(
                    "GitHub push failed (attempt %d/%d): %s. Retrying in %ds.",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    backoff,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)

        raise GitHubPushError(
            f"Failed to push {file_path} after {MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def get_cohort_size(self) -> int | None:
        """Return the cohort size published by the aggregation workflow.

        Reads ``site/data/status.json`` so the sensor matches the dashboard.
        Counting ``data/raw/`` folders over-counted aliased IDs such as the
        ``123`` test folder (#19). Returns None on any error (non-critical).
        """
        url = self._url(f"/repos/{self._repo_name}/contents/site/data/status.json")
        headers = {**self._headers, "Accept": "application/vnd.github.raw+json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status == 401:
                        raise TokenInvalidError("GitHub rejected the stored token (HTTP 401).")
                    if resp.status != 200:
                        _LOGGER.debug("Could not fetch cohort size, HTTP %s", resp.status)
                        return None
                    status = json.loads(await resp.text())
                    return int(status.get("cohort_size", 0))
        except TokenInvalidError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not fetch cohort size: %s", exc)
            return None
