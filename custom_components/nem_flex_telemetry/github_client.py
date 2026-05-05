"""GitHub client wrapper for NEM Flex Telemetry.

Handles all interaction with the central GitHub repository:
- Append-or-create JSONL files at data/raw/<household_id>/YYYY/MM/DD.jsonl
- Retry logic with exponential backoff
- Rate-limit awareness (respects X-RateLimit-Remaining)

Uses PyGithub (blocking API) run via executor to avoid blocking the event loop.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github import Repository  # type: ignore[import]

_LOGGER = logging.getLogger(__name__)

# Max retries on transient errors
MAX_RETRIES = 3
# Initial backoff in seconds (doubles each retry)
BACKOFF_INITIAL = 5
# Rate-limit threshold: pause if fewer than this many requests remain
RATE_LIMIT_PAUSE_THRESHOLD = 10


class GitHubPushError(Exception):
    """Raised when a push to GitHub fails after all retries."""


class NemFlexGitHubClient:
    """Manages JSONL file commits to the NEM Flex Telemetry central repo.

    All public methods are synchronous (blocking) and must be called via
    hass.async_add_executor_job() from async code.
    """

    def __init__(self, pat: str, repo_name: str) -> None:
        """Initialise the GitHub client.

        Args:
            pat: GitHub fine-grained personal access token with contents:write.
            repo_name: Full repo name, e.g. "purcell-lab/nem-flex-telemetry".
        """
        from github import Github, GithubException  # type: ignore[import]

        self._gh = Github(pat)
        self._repo_name = repo_name
        self._repo: Repository.Repository | None = None
        self._GithubException = GithubException

    def _get_repo(self) -> "Repository.Repository":
        """Return the cached repo object, fetching it if needed."""
        if self._repo is None:
            self._repo = self._gh.get_repo(self._repo_name)
        return self._repo

    def _check_rate_limit(self) -> None:
        """Pause if the rate limit is close to exhaustion."""
        rate_limit = self._gh.get_rate_limit()
        remaining = rate_limit.core.remaining
        reset_time = rate_limit.core.reset

        if remaining < RATE_LIMIT_PAUSE_THRESHOLD:
            wait_seconds = max(0, (reset_time - datetime.now(tz=UTC)).total_seconds()) + 5
            _LOGGER.warning(
                "GitHub rate limit low (%d remaining). Waiting %.0f seconds until reset.",
                remaining,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    def append_records(
        self,
        household_id: str,
        records: list[dict],
        commit_message: str | None = None,
    ) -> None:
        """Append a list of telemetry records to the appropriate JSONL file(s).

        Records are grouped by UTC date. If a file already exists for a given date,
        the new records are appended (not deduplicated here; deduplication happens
        in the aggregation action).

        Args:
            household_id: Household slug used as the directory name.
            records: List of validated 12-field telemetry dicts.
            commit_message: Optional commit message override.

        Raises:
            GitHubPushError: If all retries are exhausted.
        """
        if not records:
            return

        # Group records by their date (based on interval_start_utc)
        by_date: dict[str, list[dict]] = {}
        for record in records:
            dt = datetime.fromisoformat(record["interval_start_utc"].replace("Z", "+00:00"))
            date_key = dt.strftime("%Y/%m/%d")
            by_date.setdefault(date_key, []).append(record)

        for date_key, date_records in by_date.items():
            year, month, day = date_key.split("/")
            file_path = f"data/raw/{household_id}/{year}/{month}/{day}.jsonl"
            new_content = "\n".join(json.dumps(r, separators=(",", ":")) for r in date_records) + "\n"
            self._push_with_retry(
                file_path=file_path,
                new_content=new_content,
                commit_message=commit_message or f"telemetry: {household_id} {date_key} ({len(date_records)} records)",
            )

    def _push_with_retry(
        self, file_path: str, new_content: str, commit_message: str
    ) -> None:
        """Append content to a file, creating it if it does not exist.

        Retries up to MAX_RETRIES times on transient GitHub errors.
        """
        self._check_rate_limit()
        repo = self._get_repo()
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                try:
                    # File exists: get current content and append
                    existing = repo.get_contents(file_path)
                    current_content = base64.b64decode(existing.content).decode("utf-8")  # type: ignore[union-attr]
                    combined = current_content + new_content
                    repo.update_file(
                        path=file_path,
                        message=commit_message,
                        content=combined,
                        sha=existing.sha,  # type: ignore[union-attr]
                    )
                    _LOGGER.debug("Updated %s (attempt %d)", file_path, attempt)
                except self._GithubException as exc:
                    if exc.status == 404:
                        # File does not exist yet: create it
                        repo.create_file(
                            path=file_path,
                            message=commit_message,
                            content=new_content,
                        )
                        _LOGGER.debug("Created %s (attempt %d)", file_path, attempt)
                    else:
                        raise
                return  # Success

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
                    time.sleep(backoff)

        raise GitHubPushError(
            f"Failed to push {file_path} after {MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    def get_cohort_size(self) -> int:
        """Count the number of household folders in data/raw/.

        Returns 0 on any error (non-critical metric).
        """
        try:
            repo = self._get_repo()
            contents = repo.get_contents("data/raw")
            return len([c for c in contents if c.type == "dir"])  # type: ignore[union-attr]
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not fetch cohort size: %s", exc)
            return 0
