"""Appending to day files of 1 MB or more never overwrites them.

For files of 1 MB to 100 MB the Contents API JSON response carries the sha but
an empty ``content`` with ``encoding: "none"``. Treating that as an empty file
committed only the new lines with the old sha, replacing the whole day.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.nem_flex_telemetry.github_client import (
    GitHubPushError,
    NemFlexGitHubClient,
    TokenInvalidError,
)

GC = "custom_components.nem_flex_telemetry.github_client"
PATH = "data/raw/x/2026/10/12.jsonl"
RAW = "application/vnd.github.raw+json"


def _line(minute: int) -> str:
    return json.dumps({"interval_start_utc": f"2026-10-12T00:{minute:02d}:00Z", "v": minute})


EXISTING = "".join(_line(m) + "\n" for m in (0, 5, 10))
EXISTING_BYTES = EXISTING.encode("utf-8")


class _Resp:
    def __init__(self, status: int, body=None, raw: bytes | None = None) -> None:
        self.status = status
        self._body = body if body is not None else {}
        self._raw = raw

    async def json(self):
        return self._body

    async def text(self):
        return self._raw.decode() if self._raw is not None else str(self._body)

    async def read(self):
        return self._raw if self._raw is not None else b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Serves the JSON metadata, the raw body, and records every PUT."""

    def __init__(self, meta: _Resp, raw: _Resp | None = None) -> None:
        self._meta, self._raw = meta, raw
        self.raw_gets = 0
        self.put_payloads: list[dict] = []

    def get(self, url, headers=None):
        if headers and headers.get("Accept") == RAW:
            self.raw_gets += 1
            return self._raw
        return self._meta

    def put(self, url, json=None):
        self.put_payloads.append(json)
        return _Resp(200)

    def committed_text(self) -> str:
        return base64.b64decode(self.put_payloads[-1]["content"]).decode()


def _large_meta(size: int = len(EXISTING_BYTES)) -> _Resp:
    """What GitHub returns for a 1-100 MB file: sha, no content."""
    return _Resp(200, {"sha": "abc", "size": size, "encoding": "none", "content": ""})


def _client() -> NemFlexGitHubClient:
    return NemFlexGitHubClient(token="t", repo_name="purcell-lab/nem-flex-telemetry")


async def test_large_file_append_keeps_existing_lines() -> None:
    """A large day file is fetched raw and the new line is appended, not substituted."""
    session = _Session(_large_meta(), _Resp(200, raw=EXISTING_BYTES))
    await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.raw_gets == 1
    assert session.put_payloads[-1]["sha"] == "abc"
    assert session.committed_text() == EXISTING + _line(15) + "\n"


async def test_large_file_dedupes_against_full_content() -> None:
    """Lines already in a large file are not pushed again; no commit at all."""
    session = _Session(_large_meta(), _Resp(200, raw=EXISTING_BYTES))
    await _client()._push_with_retry(session, PATH, _line(5) + "\n", "m")
    assert session.put_payloads == []


async def test_raw_fetch_failure_never_puts() -> None:
    """If the full content cannot be read, nothing is committed."""
    session = _Session(_large_meta(), _Resp(500, {"message": "boom"}))
    with patch(f"{GC}.asyncio.sleep", AsyncMock()):
        with pytest.raises(GitHubPushError):
            await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.put_payloads == []


async def test_truncated_raw_body_never_puts() -> None:
    """A raw body shorter than the reported size is refused, not appended to."""
    session = _Session(_large_meta(), _Resp(200, raw=EXISTING_BYTES[:-20]))
    with patch(f"{GC}.asyncio.sleep", AsyncMock()):
        with pytest.raises(GitHubPushError, match="expected"):
            await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.put_payloads == []


async def test_raw_fetch_401_is_token_error() -> None:
    """A 401 on the raw fetch triggers re-auth like any other 401."""
    session = _Session(_large_meta(), _Resp(401))
    with pytest.raises(TokenInvalidError):
        await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.put_payloads == []


async def test_small_file_still_uses_inline_content() -> None:
    """Files under 1 MB keep the inline base64 path, with no extra request."""
    meta = _Resp(
        200,
        {
            "sha": "abc",
            "size": len(EXISTING_BYTES),
            "encoding": "base64",
            "content": base64.encodebytes(EXISTING_BYTES).decode(),
        },
    )
    session = _Session(meta)
    await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.raw_gets == 0
    assert session.committed_text() == EXISTING + _line(15) + "\n"


async def test_empty_existing_file_takes_new_content() -> None:
    """A genuinely empty file (size 0) is written with just the new lines."""
    meta = _Resp(200, {"sha": "abc", "size": 0, "encoding": "base64", "content": ""})
    session = _Session(meta)
    await _client()._push_with_retry(session, PATH, _line(15) + "\n", "m")
    assert session.raw_gets == 0
    assert session.committed_text() == _line(15) + "\n"
