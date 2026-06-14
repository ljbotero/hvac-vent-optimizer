"""Minor fixes: manifest metadata and a missing options translation step."""

from __future__ import annotations

import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer"


def _load(name):
    return json.loads((_ROOT / name).read_text(encoding="utf-8"))


def test_manifest_has_integration_type():
    manifest = _load("manifest.json")
    assert manifest.get("integration_type") == "hub"


def test_manifest_declares_loggers():
    manifest = _load("manifest.json")
    loggers = manifest.get("loggers")
    assert isinstance(loggers, list)
    assert "custom_components.hvac_vent_optimizer" in loggers


def test_options_has_manual_vents_step():
    strings = _load("translations/en.json")
    steps = strings["options"]["step"]
    assert "manual_vents" in steps, "options flow has a manual_vents step but no translation"
    assert steps["manual_vents"].get("title")
