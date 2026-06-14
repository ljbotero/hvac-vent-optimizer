"""Smoke test: every component module imports under the stub harness.

This guards against wrong-import-location regressions (e.g. importing a symbol
from a module that doesn't export it) that would otherwise only surface at
runtime on the real Home Assistant instance.
"""

from __future__ import annotations

import importlib

import pytest

PLATFORM_MODULES = [
    "hvac_vent_optimizer",  # package __init__ (pulls api/const/coordinator/services)
    "hvac_vent_optimizer.api",
    "hvac_vent_optimizer.const",
    "hvac_vent_optimizer.coordinator",
    "hvac_vent_optimizer.climate",
    "hvac_vent_optimizer.number",
    "hvac_vent_optimizer.sensor",
    "hvac_vent_optimizer.binary_sensor",
    "hvac_vent_optimizer.switch",
    "hvac_vent_optimizer.cover",
    "hvac_vent_optimizer.services",
    "hvac_vent_optimizer.config_flow",
    "hvac_vent_optimizer.utils",
]


@pytest.mark.parametrize("module", PLATFORM_MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_const_domain():
    from hvac_vent_optimizer import const

    assert const.DOMAIN == "hvac_vent_optimizer"
