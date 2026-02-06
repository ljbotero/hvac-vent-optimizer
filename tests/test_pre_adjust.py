from custom_components.hvac_vent_optimizer.dab import DEFAULT_SETTINGS, should_pre_adjust


def test_should_pre_adjust_cooling_threshold():
    setpoint = 22.0
    assert should_pre_adjust("cooling", setpoint, 22.6, DEFAULT_SETTINGS)
    assert not should_pre_adjust("cooling", setpoint, 21.0, DEFAULT_SETTINGS)


def test_should_pre_adjust_heating_threshold():
    setpoint = 22.0
    assert should_pre_adjust("heating", setpoint, 21.4, DEFAULT_SETTINGS)
    assert not should_pre_adjust("heating", setpoint, 23.0, DEFAULT_SETTINGS)
