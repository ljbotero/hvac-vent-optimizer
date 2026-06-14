"""HVAC Vent Optimizer integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FlairApi
from .const import (
    BRAND_FLAIR,
    CONF_ADJUSTMENT_WINDOW_MINUTES,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_CONTROL_STRATEGY,
    CONF_DEADBAND_PERCENT,
    CONF_DEVIATION_THRESHOLD,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
    CONF_MAX_RECALC_PER_CYCLE,
    CONF_TEMP_ERROR_OVERRIDE,
    CONF_VENT_BRAND,
    DEFAULT_ADJUSTMENT_WINDOW_MINUTES,
    DEFAULT_DEADBAND_PERCENT,
    DEFAULT_DEVIATION_THRESHOLD,
    DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
    DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
    DEFAULT_MAX_RECALC_PER_CYCLE,
    DEFAULT_TEMP_ERROR_OVERRIDE,
    DOMAIN,
    LEGACY_DEFAULT_CONTROL_STRATEGY,
    PLATFORMS,
)
from .coordinator import FlairCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

# New option keys and their defaults — migrate existing entries that lack them
_OPTIONS_DEFAULTS: dict[str, int | float] = {
    CONF_TEMP_ERROR_OVERRIDE: DEFAULT_TEMP_ERROR_OVERRIDE,
    CONF_DEADBAND_PERCENT: DEFAULT_DEADBAND_PERCENT,
    CONF_DEVIATION_THRESHOLD: DEFAULT_DEVIATION_THRESHOLD,
    CONF_MAX_RECALC_PER_CYCLE: DEFAULT_MAX_RECALC_PER_CYCLE,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE: DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW: DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
    CONF_ADJUSTMENT_WINDOW_MINUTES: DEFAULT_ADJUSTMENT_WINDOW_MINUTES,
}

# Old default that should be upgraded
_OLD_TEMP_ERROR_OVERRIDE = 0.6


async def _async_migrate_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Add missing option keys introduced in the algorithm-improvement update."""
    opts = dict(entry.options)
    changed = False

    for key, default in _OPTIONS_DEFAULTS.items():
        if key not in opts:
            opts[key] = default
            changed = True

    # Force-upgrade temp_error_override from old default 0.6 → 1.0
    current_override = opts.get(CONF_TEMP_ERROR_OVERRIDE)
    if current_override is not None:
        try:
            if float(current_override) == _OLD_TEMP_ERROR_OVERRIDE:
                opts[CONF_TEMP_ERROR_OVERRIDE] = DEFAULT_TEMP_ERROR_OVERRIDE
                changed = True
                _LOGGER.info(
                    "Migrated %s from %.1f to %.1f for entry %s",
                    CONF_TEMP_ERROR_OVERRIDE,
                    _OLD_TEMP_ERROR_OVERRIDE,
                    DEFAULT_TEMP_ERROR_OVERRIDE,
                    entry.entry_id,
                )
        except (TypeError, ValueError):
            pass

    if changed:
        hass.config_entries.async_update_entry(entry, options=opts)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    v1 → v2 (Task 27): the integration-wide default control strategy flips to
    ``balance`` (R16.1/R17.1). To honour R17.3 ("no silent override of an
    explicitly set strategy") existing installs are preserved here rather than
    by the global default change:

    * If the v1 entry already carries an explicit ``CONF_CONTROL_STRATEGY`` we
      leave it untouched — the homeowner's choice wins.
    * If it never set one, it was implicitly running the *legacy* default
      (``hybrid``). We pin that explicitly so the upgrade does not silently
      switch the running behaviour to ``balance``.

    New installs are created at the current version and therefore never reach
    this migration, so they fall through to the new ``balance`` default.
    """
    if entry.version < 2:
        opts = dict(entry.options)
        if CONF_CONTROL_STRATEGY not in opts:
            opts[CONF_CONTROL_STRATEGY] = LEGACY_DEFAULT_CONTROL_STRATEGY
            _LOGGER.info(
                "Pinned %s=%s for pre-balance entry %s (preserving prior "
                "behaviour on upgrade; balance is the new-install default)",
                CONF_CONTROL_STRATEGY,
                LEGACY_DEFAULT_CONTROL_STRATEGY,
                entry.entry_id,
            )
        hass.config_entries.async_update_entry(entry, options=opts, version=2)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HVAC Vent Optimizer from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    await _async_migrate_options(hass, entry)

    brand = entry.options.get(CONF_VENT_BRAND, entry.data.get(CONF_VENT_BRAND, BRAND_FLAIR))
    api = None
    if brand == BRAND_FLAIR:
        session = async_get_clientsession(hass)
        api = FlairApi(
            session,
            entry.data[CONF_CLIENT_ID],
            entry.data[CONF_CLIENT_SECRET],
        )

    coordinator = FlairCoordinator(hass, api, entry)
    await coordinator.async_initialize()
    await coordinator.async_ensure_structure_mode()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_detect_active_hvac()
    await coordinator.async_setup_thermostat_listeners()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator:
            coordinator.async_shutdown()
        await async_unregister_services(hass)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)
