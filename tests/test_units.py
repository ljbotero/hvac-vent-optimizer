from custom_components.hvac_vent_optimizer.utils import is_fahrenheit_unit


def test_is_fahrenheit_unit_accepts_common_forms():
    assert is_fahrenheit_unit("F")
    assert is_fahrenheit_unit("fahrenheit")
    assert is_fahrenheit_unit("\u00b0F")


def test_is_fahrenheit_unit_rejects_celsius_and_none():
    assert not is_fahrenheit_unit("C")
    assert not is_fahrenheit_unit("\u00b0C")
    assert not is_fahrenheit_unit(None)
