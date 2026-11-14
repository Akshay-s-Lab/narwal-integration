"""The Narwal app's start options and robot settings."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.narwal.narwal_client.proto import decode

from .test_integration import VACUUM, _eid, setup  # noqa: F401


def _last_task(robot) -> dict:
    return decode("StartClean_Request", robot.requests("clean/start_clean")[-1])["task"]


async def _call(hass: HomeAssistant, domain: str, service: str, data: dict) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def test_freo_mind_lets_the_robot_choose(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _call(hass, "select", "select_option", {"entity_id": _eid(hass, "select", "clean_mode"), "option": "Freo Mind"})
    await _call(hass, "vacuum", "start", {"entity_id": VACUUM})
    task = _last_task(robot)
    assert task["option"] == {"enableSmartMode": True}
    assert task["items"][0]["param"] == {"mode": "CLEAN_MODE_SWEEP_MOP_SYNC"}


async def test_next_clean_options(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _call(hass, "select", "select_option", {"entity_id": _eid(hass, "select", "clean_mode"), "option": "Vacuum then Mop"})
    await _call(hass, "number", "set_value", {"entity_id": _eid(hass, "number", "clean_passes"), "value": 2})
    await _call(hass, "number", "set_value", {"entity_id": _eid(hass, "number", "mop_passes"), "value": 3})
    await _call(hass, "select", "select_option", {"entity_id": _eid(hass, "select", "coverage"), "option": "intensive"})
    await _call(hass, "switch", "turn_on", {"entity_id": _eid(hass, "switch", "stain_remover")})
    await _call(hass, "vacuum", "set_fan_speed", {"entity_id": VACUUM, "fan_speed": "Ultra"})
    await _call(hass, "vacuum", "start", {"entity_id": VACUUM})
    task = _last_task(robot)
    assert "option" not in task
    assert task["items"][0]["param"] == {
        "mode": "CLEAN_MODE_SWEEP_THEN_MOP", "fanLevel": "FAN_LEVEL_SUPER", "mopHumidity": "MOP_HUMIDITY_NORMAL",
        "sweepTime": 2, "mopTime": 3, "overlapLevel": "OVERLAP_LEVEL_DENSE", "enableHeavyDirtyClean": True,
    }


async def test_customize_rooms(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _call(hass, "vacuum", "send_command", {"entity_id": VACUUM, "command": "clean_rooms", "params": {
        "mode": "Vacuum Only", "fan_speed": "Quiet",
        "rooms": [{"room": 2, "mode": "Mop Only", "mop_humidity": "wet", "passes": 2}, 1],
    }})
    first, second = _last_task(robot)["items"]
    assert (first["zone"]["zoneId"], first["order"], second["zone"]["zoneId"], second["order"]) == (2, 1, 1, 2)
    assert first["param"] == {
        "mode": "CLEAN_MODE_MOP", "mopHumidity": "MOP_HUMIDITY_WET", "mopTime": 2, "overlapLevel": "OVERLAP_LEVEL_NORMAL",
    }
    assert second["param"] == {
        "mode": "CLEAN_MODE_SWEEP", "fanLevel": "FAN_LEVEL_MUTE", "sweepTime": 1, "overlapLevel": "OVERLAP_LEVEL_NORMAL",
    }
    for bad in (
        {"rooms": [{"room": 1, "mode": "Freo Mind"}]},
        {"rooms": [1], "coverage": "extreme"},
        {"rooms": [1], "passes": 4},
    ):
        with pytest.raises(ServiceValidationError):
            await _call(hass, "vacuum", "send_command", {"entity_id": VACUUM, "command": "clean_rooms", "params": bad})


async def test_room_order(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    await _call(hass, "vacuum", "send_command",
                {"entity_id": VACUUM, "command": "set_room_order", "params": {"rooms": [1, 2, 3]}})
    assert robot.config["customCleanStrategy"]["cleanOrders"] == [
        {"isModified": True, "roomCleanOrder": [1, 2, 3], "defaultRoomCleanOrder": [3, 2, 1], "mapId": 1},
    ]
    await _call(hass, "vacuum", "start", {"entity_id": VACUUM})
    assert [i["zone"]["zoneId"] for i in _last_task(robot)["items"]] == [1, 2]
    with pytest.raises(ServiceValidationError):
        await _call(hass, "vacuum", "send_command",
                    {"entity_id": VACUUM, "command": "set_room_order", "params": {"rooms": [9]}})


async def test_robot_settings(hass: HomeAssistant, setup, robot) -> None:  # noqa: F811
    corner = _eid(hass, "select", "corner_cleaning")
    assert hass.states.get(corner).state == "every_day"
    await _call(hass, "select", "select_option", {"entity_id": corner, "option": "every_7_days"})
    assert robot.config["cleanCorner"] == {"cleanCornerFreq": "CLEAN_CORNER_FREQ_EVERY_N_DAYS", "interval": 7}
    assert hass.states.get(corner).state == "every_7_days"

    suction = _eid(hass, "select", "freo_suction")
    coverage = _eid(hass, "select", "freo_coverage")
    assert (hass.states.get(suction).state, hass.states.get(coverage).state) == ("smart", "intensive")
    await _call(hass, "select", "select_option", {"entity_id": suction, "option": "turbo"})
    assert robot.config["smartCleanParam"] == {
        "overlapLevelStrategy": {"isCustomizeMode": True, "overlapLevel": "OVERLAP_LEVEL_DENSE"},
        "fanLevelStrategy": {"isCustomizeMode": True, "fanLevel": "FAN_LEVEL_STRONG"},
    }
    await _call(hass, "select", "select_option", {"entity_id": coverage, "option": "smart"})
    assert robot.config["smartCleanParam"]["overlapLevelStrategy"] == {}

    await _call(hass, "select", "select_option",
                {"entity_id": _eid(hass, "select", "obstacle_avoidance"), "option": "safer"})
    assert robot.config["avoidMode"] == "AVOID_MODE_SAFER"
    await _call(hass, "switch", "turn_on", {"entity_id": _eid(hass, "switch", "dust_bag_drying")})
    assert robot.config["dryRobotBagEnabled"] is True
