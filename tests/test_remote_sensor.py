from custom_components.hvac_vent_optimizer.utils import get_remote_sensor_id


def test_get_remote_sensor_id_from_list():
    room = {"relationships": {"remote-sensors": {"data": [{"id": "remote-1"}]}}}
    assert get_remote_sensor_id(room) == "remote-1"


def test_get_remote_sensor_id_from_dict():
    room = {"relationships": {"remote-sensors": {"data": {"id": "remote-2"}}}}
    assert get_remote_sensor_id(room) == "remote-2"


def test_get_remote_sensor_id_handles_missing():
    assert get_remote_sensor_id({}) is None
    assert get_remote_sensor_id({"relationships": {}}) is None
    room = {"relationships": {"remote-sensors": {"data": []}}}
    assert get_remote_sensor_id(room) is None
