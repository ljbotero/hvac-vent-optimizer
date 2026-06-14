"""Fix #6: manual vent number entity must restore via RestoreNumber.

The previous code fell back to RestoreEntity on ImportError, but
async_added_to_hass() calls async_get_last_number_data(), which RestoreEntity
does not provide — that fallback would raise. Guard the restore contract.
"""

from __future__ import annotations


def test_manual_number_inherits_restorenumber():
    from homeassistant.components.number import RestoreNumber

    from hvac_vent_optimizer.number import ManualVentApertureNumber

    assert issubclass(ManualVentApertureNumber, RestoreNumber)


def test_restore_base_supports_last_number_data():
    from hvac_vent_optimizer.number import ManualVentApertureNumber

    # Whatever base supplies restore behavior must expose the method the
    # entity actually calls in async_added_to_hass.
    assert hasattr(ManualVentApertureNumber, "async_get_last_number_data")
