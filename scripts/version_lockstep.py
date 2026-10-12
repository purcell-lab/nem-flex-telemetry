#!/usr/bin/env python3
"""Fail when the integration's version is not the same everywhere it appears.

Three places carry the version: manifest.json (what Home Assistant and HACS
read), VERSION in const.py (what the integration logs and reports), and the
README status line (what users read). The release tag must match them too: a
release cut from a manifest that still says the previous version installs as
that previous version.

Usage:
    python scripts/version_lockstep.py              # manifest vs const.py vs README
    python scripts/version_lockstep.py --tag v0.7.2 # also vs a release tag
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "nem_flex_telemetry"
MANIFEST = COMPONENT / "manifest.json"
CONST = COMPONENT / "const.py"
README = ROOT / "README.md"


def manifest_version(text: str) -> str:
    return str(json.loads(text)["version"])


def const_version(text: str) -> str:
    m = re.search(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"', text, re.MULTILINE)
    if not m:
        raise ValueError('const.py has no VERSION = "X.Y.Z" line')
    return m.group(1)


def readme_version(text: str) -> str:
    m = re.search(r"\*\*Status: v(\d+\.\d+\.\d+)\.\*\*", text)
    if not m:
        raise ValueError("README has no '**Status: vX.Y.Z.**' line")
    return m.group(1)


def tag_version(tag: str) -> str:
    """'v0.7.2' or '0.7.2' -> '0.7.2'; anything else is an error."""
    m = re.fullmatch(r"v?(\d+\.\d+\.\d+)", tag.strip())
    if not m:
        raise ValueError(f"release tag {tag!r} is not vX.Y.Z")
    return m.group(1)


def check(
    manifest_text: str, const_text: str, readme_text: str, tag: str | None = None
) -> list[str]:
    """Return the mismatches, empty when everything is in step."""
    problems: list[str] = []
    manifest = manifest_version(manifest_text)
    const = const_version(const_text)
    readme = readme_version(readme_text)
    if const != manifest:
        problems.append(f"const.py VERSION is {const} but manifest.json says {manifest}")
    if readme != manifest:
        problems.append(f"README status line is v{readme} but manifest.json says {manifest}")
    if tag is not None:
        tagged = tag_version(tag)
        if tagged != manifest:
            problems.append(f"release tag is {tag} but manifest.json says {manifest}")
    return problems


def main(argv: list[str]) -> int:
    tag = None
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
    problems = check(MANIFEST.read_text(), CONST.read_text(), README.read_text(), tag)
    for problem in problems:
        print(f"version_lockstep: {problem}")
    if not problems:
        print(f"version_lockstep: {manifest_version(MANIFEST.read_text())} everywhere")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
