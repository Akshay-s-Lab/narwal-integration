"""Map editing, mapping and manual control through vacuum.send_command."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.narwal.narwal_client.proto import decode

from .test_integration import VACUUM, setup  # noqa: F401


async def _send(hass: HomeAssistant, command: str, params: dict | None = None) -> None:
    await hass.services.async_call(
        "vacuum", "send_command", {"entity_id": VACUUM, "command": command, "params": params or {}}, blocking=True,
    )


def _zones(hass: HomeAssistant) -> list[dict]:
    return hass.states.get(VACUUM).attributes["no_go_zones"]


async def test_no_go_zone_add_and_remove(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    assert _zones(hass) == []
    await _send(hass, "add_no_go_zone", {"zone": [-6, -2, -2, 2], "kind": "mop"})
    request = decode("MapOperation_Request", robot.requests("map/operation")[-1])
    assert (request["mapId"], request["mapVersion"], request["editVersion"]) == (1, 3, 7)
    forbidden = request["operations"][0]["forbiddenAreaOperation"]
    assert forbidden["operation"] == "OPERATION_SET"
    assert forbidden["forbidden"]["type"] == "FORBIDDEN_TYPE_MOP" and forbidden["forbidden"]["id"] == 1
    await hass.async_block_till_done()
    assert _zones(hass) == [{"id": 1, "kind": "mop", "points": [[-6.0, -2.0], [-2.0, -2.0], [-2.0, 2.0], [-6.0, 2.0]]}]
    assert hass.states.get(VACUUM).attributes["no_go_rooms"] == [3]

    await _send(hass, "add_barrier", {"start": [0, 0], "end": [4, 0]})
    await hass.async_block_till_done()
    barrier = _zones(hass)[1]
    assert barrier["id"] == 2 and barrier["kind"] == "all"
    assert barrier["points"] == [[0.0, 0.5], [4.0, 0.5], [4.0, -0.5], [0.0, -0.5]]

    await _send(hass, "remove_no_go_zone", {"id": 1})
    await hass.async_block_till_done()
    assert [z["id"] for z in _zones(hass)] == [2]
    assert robot.edit_version == 10

    for bad in ({"zone": [1, 1, 1, 5]}, {"points": [[0, 0], [1, 1]]}, {"zone": [0, 0, 2, 2], "kind": "lava"}):
        with pytest.raises((ServiceValidationError, Exception)):
            await _send(hass, "add_no_go_zone", bad)


async def test_rooms_rename_merge_split(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _send(hass, "rename_room", {"room": 1, "name": "Lounge"})
    request = decode("MapOperation_Request", robot.requests("map/operation")[-1])
    assert request["operations"][0]["roomInfoOperation"]["roomInfo"] == {"roomId": 1, "type": 3, "customName": "Lounge"}
    await hass.async_block_till_done()
    assert hass.states.get(VACUUM).attributes["rooms"][1] == "Lounge"

    await _send(hass, "merge_rooms", {"rooms": [1, 2]})
    merge = decode("MapOperation_Request", robot.requests("map/operation")[-1])["operations"][0]
    assert merge == {"roomMergeOperation": {"roomId1": 1, "roomId2": 2}}

    await _send(hass, "split_room", {"room": 3, "start": [1.4, -3], "end": [1.6, 3]})
    split = decode("MapOperation_Request", robot.requests("map/operation")[-1])["operations"][0]["roomSegOperation"]
    assert split == {"startSegPoint": {"x": 1, "y": -3}, "endSegPoint": {"x": 2, "y": 3}, "leftRoomId": 3}
    await hass.async_block_till_done()
    assert sorted(hass.states.get(VACUUM).attributes["rooms"]) == [1, 3, 4]

    with pytest.raises(ServiceValidationError):
        await _send(hass, "merge_rooms", {"rooms": [1]})


async def test_mapping_needs_confirmation(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    with pytest.raises(ServiceValidationError):
        await _send(hass, "start_mapping")
    assert not robot.requests("map/build_map/start")
    await _send(hass, "start_mapping", {"replace_current_map": True})
    request = decode("BuildMap_Request", robot.requests("map/build_map/start")[-1])
    assert request == {"buildType": "MAP_BUILD_TYPE_MAIN_MAP", "isRebuildCurrentMap": True}
    await _send(hass, "save_map")
    assert robot.requests("map/save_map_update_info")


async def test_manual_control(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _send(hass, "manual_control", {"enabled": True})
    mode = decode("SetManualControlMode_Request", robot.requests("telecontrol/set_manual_control_mode")[-1])
    assert mode == {"manualControlMode": "MANUAL_CONTROL_MODE_JOYSTICK"}
    await _send(hass, "drive", {"linear": 150, "angular": -30})
    velocity = decode("TelecontrolVelocity", robot.requests("telecontrol/velocity_control")[-1])
    assert velocity == {"linearVelocity": 100, "angularVelocity": -30, "type": "VELOCITY_TYPE_PERCENTAGE"}
    await _send(hass, "manual_control", {"enabled": False})
    mode = decode("SetManualControlMode_Request", robot.requests("telecontrol/set_manual_control_mode")[-1])
    assert mode == {"manualControlMode": "MANUAL_CONTROL_MODE_OFF"}
