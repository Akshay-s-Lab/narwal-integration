"""End-to-end tests of the Narwal integration against a fake robot."""

from __future__ import annotations

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.narwal.narwal_client.proto import decode

from .conftest import push

VACUUM = "vacuum.narwal_ehf6crnrgt_narwal_vacuum"


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, "narwal", f"abcdef1234567890abcdef1234567890_{key}")
    assert entity_id, f"no {platform} entity for {key}"
    return entity_id


@pytest.fixture
async def setup(hass: HomeAssistant, config_entry, mock_robot):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return config_entry.runtime_data


async def test_setup_and_states(hass: HomeAssistant, setup, robot) -> None:
    state = hass.states.get(VACUUM)
    assert state.state == VacuumActivity.DOCKED
    assert state.attributes["fan_speed"] == "Normal"
    assert state.attributes["rooms"] == {1: "Living Room", 2: "Galley", 3: "Toilet"}
    assert state.attributes["status"] == "Docked"
    assert state.attributes["no_go_rooms"] == [3]

    assert hass.states.get(_eid(hass, "sensor", "battery")).state == "87.0"
    assert hass.states.get(_eid(hass, "sensor", "detergent")).state == "installed_and_empty"
    assert hass.states.get(_eid(hass, "sensor", "consumables_due")).state == "1"
    assert hass.states.get(_eid(hass, "binary_sensor", "docked")).state == "on"
    assert hass.states.get(_eid(hass, "binary_sensor", "maintenance_due")).state == "on"
    assert hass.states.get(_eid(hass, "switch", "auto_empty")).state == "on"
    assert hass.states.get(_eid(hass, "switch", "child_lock")).state == "off"
    assert hass.states.get(_eid(hass, "switch", "do_not_disturb")).attributes["start"] == "21:00"
    assert hass.states.get(_eid(hass, "number", "volume")).state == "16.0"
    assert hass.states.get(_eid(hass, "select", "mop_dry_strength")).state == "smart"
    assert hass.states.get(_eid(hass, "select", "mop_humidity")).state == "normal"

    device = setup.client.state
    assert device.firmware_version == "v01.09.01.00"
    assert device.no_go_room_ids == {3}


async def test_start_skips_no_go_rooms(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call("vacuum", "start", {"entity_id": VACUUM}, blocking=True)
    req = decode("StartClean_Request", robot.requests("clean/start_clean")[-1])
    items = req["task"]["items"]
    assert [i["zone"]["zoneId"] for i in items] == [1, 2]
    assert items[0]["param"]["mode"] == "CLEAN_MODE_SWEEP_MOP_SYNC"
    assert items[0]["param"]["sweepMopSyncTime"] == 1
    assert req["task"]["taskType"] == "TASK_TYPE_CUSTOM_CLEAN"


async def test_fan_speed_while_docked_applies_to_next_clean(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call(
        "vacuum", "set_fan_speed", {"entity_id": VACUUM, "fan_speed": "Max"}, blocking=True
    )
    assert not robot.requests("clean/set_fan_level")  # robot rejects this while docked
    assert hass.states.get(VACUUM).attributes["fan_speed"] == "Max"

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, "select", "clean_mode"), "option": "Vacuum Only"}, blocking=True,
    )
    await hass.services.async_call("vacuum", "start", {"entity_id": VACUUM}, blocking=True)
    param = decode("StartClean_Request", robot.requests("clean/start_clean")[-1])["task"]["items"][0]["param"]
    assert param == {"mode": "CLEAN_MODE_SWEEP", "fanLevel": "FAN_LEVEL_SUPER", "sweepTime": 1}


async def test_clean_rooms_command(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call(
        "vacuum", "send_command",
        {"entity_id": VACUUM, "command": "clean_rooms",
         "params": {"rooms": [2], "mode": "Vacuum then Mop", "fan_speed": "Strong",
                    "mop_humidity": "wet", "passes": 2}},
        blocking=True,
    )
    item = decode("StartClean_Request", robot.requests("clean/start_clean")[-1])["task"]["items"][0]
    assert item["zone"] == {"zoneType": "ZONE_TYPE_ROOM", "zoneId": 2}
    assert item["param"] == {
        "mode": "CLEAN_MODE_SWEEP_THEN_MOP", "fanLevel": "FAN_LEVEL_STRONG",
        "mopHumidity": "MOP_HUMIDITY_WET", "sweepTime": 2, "mopTime": 2,
    }
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "vacuum", "send_command",
            {"entity_id": VACUUM, "command": "clean_rooms", "params": {"rooms": [99]}}, blocking=True,
        )


async def test_clean_zones_command(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call(
        "vacuum", "send_command",
        {"entity_id": VACUUM, "command": "clean_zones",
         "params": {"zones": [[13, -2, -5, 16], [0, 0, 18.4, 17.6]], "mode": "Vacuum Only", "passes": 2}},
        blocking=True,
    )
    task = decode("StartClean_Request", robot.requests("clean/start_clean")[-1])["task"]
    assert task["taskType"] == "TASK_TYPE_GENERAL_SWEEP" and task["mapId"] == 1
    assert task["option"] == {"enableSmartMode": True}
    first, second = task["items"]
    assert first["zone"]["zoneType"] == "ZONE_TYPE_ZONE" and first["zone"]["zoneId"] == 1
    assert first["zone"]["selectionBounds"] == [
        {"x": -5, "y": 16}, {"x": 13, "y": 16}, {"x": 13, "y": -2}, {"x": -5, "y": -2},
    ]
    path = first["zone"]["cleaningBounds"]
    assert path[0] == {"x": -4, "y": -1} and path[16] == {"x": 12, "y": -1} and path[17] == {"x": 12, "y": 1}
    assert all(-5 < p["x"] < 13 and -2 < p["y"] < 16 for p in path)
    assert first["param"] == {
        "mode": "CLEAN_MODE_SWEEP", "fanLevel": "FAN_LEVEL_NORMAL", "sweepTime": 2,
        "overlapLevel": "OVERLAP_LEVEL_NORMAL", "enableSmartMode": True,
    }
    assert first["order"] == 1 and second["order"] == 2
    assert second["zone"]["selectionBounds"][1] == {"x": 18, "y": 18}

    with pytest.raises(HomeAssistantError, match="too small"):
        await hass.services.async_call(
            "vacuum", "send_command",
            {"entity_id": VACUUM, "command": "clean_zones", "params": {"zones": [[0, 0, 10, 8]]}}, blocking=True,
        )

    for bad in ([], [[1, 2, 3]], [[1, 1, 1, 5]], [["a", 1, 2, 3]]):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "vacuum", "send_command",
                {"entity_id": VACUUM, "command": "clean_zones", "params": {"zones": bad}}, blocking=True,
            )


async def test_robot_position_attribute(hass: HomeAssistant, setup) -> None:
    assert hass.states.get(VACUUM).attributes["robot_position"] == [-8, 0]


async def test_clean_area_segments(hass: HomeAssistant, setup, robot) -> None:
    entity = hass.data["vacuum"].get_entity(VACUUM)
    segments = await entity.async_get_segments()
    assert [(s.id, s.name) for s in segments] == [("1", "Living Room"), ("2", "Galley"), ("3", "Toilet")]
    await entity.async_clean_segments(["2"])
    req = decode("StartClean_Request", robot.requests("clean/start_clean")[-1])
    assert [i["zone"]["zoneId"] for i in req["task"]["items"]] == [2]


async def test_consumable_life_tracks_cleaning_time(hass: HomeAssistant, setup, robot, config_entry) -> None:
    client = setup.client
    filter_life = _eid(hass, "sensor", "dust_filter_life")
    roller_life = _eid(hass, "sensor", "roller_brush_life")
    assert hass.states.get(filter_life).state == "100"

    composite = {"task": "TASK_TYPE_COMPOSITE_CLEAN", "compositeCleanStatus": "COMPOSITE_CLEAN_STATUS_ROOM_SWEEP"}
    push(hass, client, "status/robot_base_status", "RobotBaseStatus", {"robotTaskStatus": composite})
    push(hass, client, "status/working_status", "WorkingStatus", {"timeConsuming": 15 * 3600})
    push(hass, client, "status/working_status", "WorkingStatus", {"cleaningZoneId": 1})
    push(hass, client, "status/robot_base_status", "RobotBaseStatus",
         {"robotTaskStatus": {**composite, "compositeCleanStatus": "COMPOSITE_CLEAN_STATUS_PARKING"}})
    await hass.async_block_till_done()
    assert hass.states.get(VACUUM).state == VacuumActivity.RETURNING
    push(hass, client, "status/robot_base_status", "RobotBaseStatus",
         {"robotTaskStatus": {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_IN_STATION"},
          "stationContactType": "STATION_CONTACT_TYPE_CONTACTED"})
    await hass.async_block_till_done()

    assert hass.states.get(filter_life).state == "90"
    assert hass.states.get(roller_life).state == "95"
    attrs = hass.states.get(filter_life).attributes
    assert attrs["used_hours"] == 15.0 and attrs["lifetime_hours"] == 150 and attrs["tracking_since"]

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(filter_life).state == "90"

    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", "reset_dust_filter")}, blocking=True,
    )
    await hass.async_block_till_done()
    reset = decode("ResetConsumableInfo_Request", robot.requests("consumable/reset_consumable_info")[-1])
    assert reset == {"consumableInfo": {"replaceItems": ["CONSUMABLE_REPLACE_ITEM_DUST_FILTER"]}}
    assert hass.states.get(filter_life).state == "100"
    assert hass.states.get(roller_life).state == "95"


async def test_trail_follows_the_clean(hass: HomeAssistant, setup) -> None:
    client = setup.client
    composite = {"task": "TASK_TYPE_COMPOSITE_CLEAN", "compositeCleanStatus": "COMPOSITE_CLEAN_STATUS_ROOM_SWEEP"}

    def pose(x: float, y: float) -> None:
        push(hass, client, "map/display_map", "DisplayMap",
             {"robot": {"location": {"x": x, "y": y}, "theta": 0.0}, "currentCleanZoneId": 1})

    pose(-8.0, 0.0)
    assert client.state.trail == []

    push(hass, client, "status/robot_base_status", "RobotBaseStatus", {"robotTaskStatus": composite})
    for x, y in [(-8.0, 0.0), (-8.1, 0.1), (-6.0, 0.0), (-6.0, 2.0)]:
        pose(x, y)
    assert client.state.trail == [(-8.0, 0.0), (-6.0, 0.0), (-6.0, 2.0)]

    push(hass, client, "status/robot_base_status", "RobotBaseStatus",
         {"robotTaskStatus": {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_IN_STATION"},
          "stationContactType": "STATION_CONTACT_TYPE_CONTACTED"})
    pose(-8.0, 0.0)
    assert len(client.state.trail) == 3

    push(hass, client, "status/robot_base_status", "RobotBaseStatus", {"robotTaskStatus": composite})
    pose(-4.0, 1.0)
    assert client.state.trail == [(-4.0, 1.0)]
    await hass.async_block_till_done()

    from homeassistant.components.camera import async_get_image

    image = await async_get_image(hass, _eid(hass, "camera", "map"))
    assert image.content.startswith(b"\x89PNG")


async def test_pushes_drive_activity(hass: HomeAssistant, setup) -> None:
    client = setup.client
    cleaning = {
        "batteryPercentage": 80.0,
        "robotTaskStatus": {"task": "TASK_TYPE_COMPOSITE_CLEAN",
                            "compositeCleanStatus": "COMPOSITE_CLEAN_STATUS_ROOM_SWEEP"},
        "fanLevel": "FAN_LEVEL_STRONG",
    }
    push(hass, client, "status/robot_base_status", "RobotBaseStatus", cleaning)
    push(hass, client, "status/working_status", "WorkingStatus",
         {"timeConsuming": 120, "coveredArea": 3.5, "cleaningZoneId": 2})
    await hass.async_block_till_done()
    state = hass.states.get(VACUUM)
    assert state.state == VacuumActivity.CLEANING
    assert state.attributes["fan_speed"] == "Strong"
    assert state.attributes["current_room"] == "Galley"
    assert hass.states.get(_eid(hass, "sensor", "cleaned_area")).state == "3.5"
    assert hass.states.get(_eid(hass, "sensor", "current_room")).state == "Galley"

    paused = {**cleaning, "robotTaskStatus": {**cleaning["robotTaskStatus"], "pauseStatus": True}}
    push(hass, client, "status/robot_base_status", "RobotBaseStatus", paused)
    await hass.async_block_till_done()
    assert hass.states.get(VACUUM).state == VacuumActivity.PAUSED

    returning = {**cleaning, "robotTaskStatus": {"task": "TASK_TYPE_RECALL",
                                                  "recallStatus": "RECALL_STATUS_BACK_TO_STATION"}}
    push(hass, client, "status/robot_base_status", "RobotBaseStatus", returning)
    await hass.async_block_till_done()
    assert hass.states.get(VACUUM).state == VacuumActivity.RETURNING

    error = {**returning, "robotTaskStatus": {"task": "TASK_TYPE_IDLE",
                                               "idleStatus": "IDLE_STATUS_OUTSIDE_STATION"},
             "errorCode": [{"identityCode": 0x01010013, "level": 2}]}
    push(hass, client, "status/robot_base_status", "RobotBaseStatus", error)
    await hass.async_block_till_done()
    assert hass.states.get(VACUUM).state == VacuumActivity.ERROR
    assert "overflow" in hass.states.get(_eid(hass, "sensor", "error")).state.lower()


async def test_settings_write(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, "switch", "child_lock")}, blocking=True
    )
    assert decode("SetConfig_Request", robot.requests("config/set")[-1]) == {"childLockEnabled": True}
    assert hass.states.get(_eid(hass, "switch", "child_lock")).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, "switch", "auto_empty")}, blocking=True
    )
    # An explicit False must reach the robot (absent = "unchanged").
    assert decode("SetConfig_Request", robot.requests("config/set")[-1]) == {"dustGatheringEnabled": False}

    await hass.services.async_call(
        "number", "set_value", {"entity_id": _eid(hass, "number", "volume"), "value": 40}, blocking=True
    )
    assert decode("SetConfig_Request", robot.requests("config/set")[-1]) == {"volumePercentage": 40}

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, "select", "carpet_strategy"), "option": "avoid"}, blocking=True,
    )
    assert hass.states.get(_eid(hass, "select", "carpet_strategy")).state == "avoid"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, "switch", "do_not_disturb")}, blocking=True
    )
    sent = decode("SetConfig_Request", robot.requests("config/set")[-1])
    assert sent == {"disturbMode": {"enabled": False, "beginTime": 21 * 3600, "endTime": 9 * 3600}}


async def test_buttons_and_rejection(hass: HomeAssistant, setup, robot) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", "wash_and_dry_mop")}, blocking=True
    )
    assert robot.requests("supply/wash_and_dry_mop")

    robot.reject.add("supply/wash_mop")
    with pytest.raises(HomeAssistantError, match="rejected"):
        await hass.services.async_call(
            "button", "press", {"entity_id": _eid(hass, "button", "wash_mop")}, blocking=True
        )


async def test_map_camera(hass: HomeAssistant, setup) -> None:
    from homeassistant.components.camera import async_get_image

    image = await async_get_image(hass, _eid(hass, "camera", "map"))
    assert image.content_type == "image/png"
    assert image.content.startswith(b"\x89PNG")


async def test_unload(hass: HomeAssistant, setup, config_entry) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
