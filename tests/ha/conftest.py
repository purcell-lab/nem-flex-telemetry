"""Fixtures for tests that boot a real Home Assistant core.

Requires pytest-homeassistant-custom-component (see requirements-test.txt).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/nem_flex_telemetry."""
    yield
