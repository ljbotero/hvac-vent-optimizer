"""Task 27 — ``balance`` becomes the default strategy, with upgrade preservation.

After the R15.6 evidence gate was evaluated (Task 26), the homeowner explicitly
chose to ship ``balance`` as the default **on the vent-movement win** with the
spread gate waived (see ``docs/quality-baseline.md``). These tests lock in the
two behaviours that decision requires:

* **R16.1 / R17.1** — new installs default to ``balance``.
* **R17.3** — an existing install's *explicit* strategy is never silently
  overridden on upgrade, and a pre-``balance`` install that never explicitly
  picked a strategy is pinned to the *legacy* default (``hybrid``) rather than
  being silently flipped to ``balance``.

The upgrade-preservation seam is the config-entry migration
(:func:`hvac_vent_optimizer.async_migrate_entry`), gated on the config-flow
``VERSION`` bump (1 → 2): only entries created before the bump are migrated, so
genuinely new installs fall through to the new default.
"""

from __future__ import annotations

import asyncio

from hvac_vent_optimizer import async_migrate_entry, config_flow, const


# ---------------------------------------------------------------------------
# Minimal config-entry / hass fakes (the conftest ConfigEntry stub is empty).
# ---------------------------------------------------------------------------
class _FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[tuple] = []

    def async_update_entry(self, entry, *, options=None, version=None, **_kw):
        if options is not None:
            entry.options = dict(options)
        if version is not None:
            entry.version = version
        self.updates.append((entry.entry_id, dict(entry.options), entry.version))
        return True


class _FakeHass:
    def __init__(self) -> None:
        self.config_entries = _FakeConfigEntries()


class _Entry:
    def __init__(self, *, version: int = 1, options: dict | None = None):
        self.version = version
        self.options = dict(options or {})
        self.entry_id = "entry_test"


def _migrate(entry) -> tuple[_FakeHass, bool]:
    hass = _FakeHass()
    ok = asyncio.run(async_migrate_entry(hass, entry))
    return hass, ok


def _resolved_strategy(entry) -> str:
    """How the coordinator resolves the active strategy from an entry."""
    return entry.options.get(const.CONF_CONTROL_STRATEGY, const.DEFAULT_CONTROL_STRATEGY)


# ---------------------------------------------------------------------------
# R16.1 / R17.1 — new-install default
# ---------------------------------------------------------------------------
def test_default_strategy_is_balance():
    assert const.DEFAULT_CONTROL_STRATEGY == const.CONTROL_STRATEGY_BALANCE
    assert const.DEFAULT_CONTROL_STRATEGY == "balance"


def test_config_flow_version_bumped_for_migration():
    # The migration seam only fires for entries older than the bumped version.
    assert config_flow.HvacVentOptimizerConfigFlow.VERSION >= 2


def test_new_install_resolves_to_balance():
    """A fresh v2 entry with no explicit strategy resolves to ``balance``."""
    entry = _Entry(version=2, options={})
    # New installs are created at the current version → not migrated.
    _, ok = _migrate(entry)
    assert ok is True
    assert _resolved_strategy(entry) == "balance"


# ---------------------------------------------------------------------------
# R17.3 — preserve existing selections on upgrade
# ---------------------------------------------------------------------------
def test_upgrade_preserves_explicit_legacy_selection():
    """A v1 install that explicitly picked ``dab`` keeps ``dab`` after upgrade."""
    entry = _Entry(version=1, options={const.CONF_CONTROL_STRATEGY: "dab"})
    _, ok = _migrate(entry)
    assert ok is True
    assert entry.version == 2
    assert _resolved_strategy(entry) == "dab"


def test_upgrade_preserves_explicit_balance_selection():
    entry = _Entry(version=1, options={const.CONF_CONTROL_STRATEGY: "balance"})
    _migrate(entry)
    assert _resolved_strategy(entry) == "balance"


def test_upgrade_without_explicit_strategy_pins_legacy_default():
    """Pre-``balance`` install that never chose a strategy is NOT flipped.

    Before this release the implicit default was ``hybrid``; the migration pins
    that so the homeowner's running behaviour is preserved rather than silently
    switched to ``balance``.
    """
    entry = _Entry(version=1, options={})
    _, ok = _migrate(entry)
    assert ok is True
    assert entry.version == 2
    assert entry.options[const.CONF_CONTROL_STRATEGY] == const.LEGACY_DEFAULT_CONTROL_STRATEGY
    assert _resolved_strategy(entry) == "hybrid"


def test_migration_is_idempotent_on_v2_entries():
    """Re-running migration on an already-current entry changes nothing."""
    entry = _Entry(version=2, options={const.CONF_CONTROL_STRATEGY: "stats"})
    hass, ok = _migrate(entry)
    assert ok is True
    assert entry.options[const.CONF_CONTROL_STRATEGY] == "stats"
    assert hass.config_entries.updates == []  # no write performed
