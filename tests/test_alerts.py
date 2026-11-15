"""Alerts: event entity and phone pushes driven by robot state changes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.narwal.const import CONF_NOTIFY_TARGETS, CONF_PUSH_ALERTS

from .conftest import push

DOCKED = {
    "robotTaskStatus": {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_IN_STATION"},
    "stationContactType": "STATION_CONTACT_TYPE_CONTACTED",
    "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_NORMAL",
}
CLEANING = {
    "robotTaskStatus": {"task": "TASK_TYPE_COMPOSITE_CLEAN", "compositeCleanStatus": "COMPOSITE_CLEAN_STATUS_ROOM_SWEEP"},
    "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_NORMAL",
}


@pytest.fixture
def pushes(hass: HomeAssistant) -> list[ServiceCall]:
    calls: list[ServiceCall] = []
    for phone in ("mobile_app_phone_a", "mobile_app_phone_b"):
        hass.services.async_register("notify", phone, calls.append)
    return calls


@pytest.fixture
async def client(hass: HomeAssistant, config_entry, mock_robot, pushes):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    return config_entry.runtime_data.client


@pytest.fixture
async def base(hass: HomeAssistant, client, robot, pushes):
    """Push a base status; polls answer with the same status."""

    async def send(values: dict) -> None:
        robot.base = dict(values)
        push(hass, client, "status/robot_base_status", "RobotBaseStatus", values)
        await hass.async_block_till_done()

    await send(DOCKED)
    pushes.clear()
    return send


def alerts_entity(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "event", "narwal", "abcdef1234567890abcdef1234567890_alerts"
    )
    assert entity_id
    return entity_id


async def test_clean_finished_reports_area_and_time(hass: HomeAssistant, client, base, pushes) -> None:
    await base(CLEANING)
    await hass.async_block_till_done()
    assert hass.states.get(alerts_entity(hass)).attributes["event_type"] == "clean_started"
    assert {c.service for c in pushes} == {"mobile_app_phone_a", "mobile_app_phone_b"}

    push(hass, client, "status/working_status", "WorkingStatus", {"timeConsuming": 42 * 60, "coveredArea": 61.5})
    await hass.async_block_till_done()
    await base(CLEANING)
    await base({**DOCKED, "terminateReason": "TASK_RESULT_NORMAL_END"})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()

    state = hass.states.get(alerts_entity(hass))
    assert state.attributes["event_type"] == "clean_finished"
    assert state.attributes["message"] == "Cleaning finished: 61.5 m² in 42 min."
    assert pushes[-1].data["message"] == "Cleaning finished: 61.5 m² in 42 min."
    assert pushes[-1].data["data"]["tag"] == "narwal_clean_finished"


async def test_low_battery_end_is_reported_as_early(hass: HomeAssistant, client, base, pushes) -> None:
    await base(CLEANING)
    await base({**DOCKED, "terminateReason": "TASK_RESULT_LOW_BATTERY_FORCE_END"})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    state = hass.states.get(alerts_entity(hass))
    assert state.attributes["event_type"] == "clean_ended_early"
    assert "battery ran low" in state.attributes["message"]


async def test_tank_and_error_alerts(hass: HomeAssistant, client, base, pushes) -> None:
    await base({**DOCKED, "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_EMPTY"})
    await hass.async_block_till_done()
    state = hass.states.get(alerts_entity(hass))
    assert state.attributes["event_type"] == "clean_water_tank"
    assert pushes[-1].data["message"] == "The clean water tank is empty. Refill it."

    await base({**DOCKED, "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_EMPTY"})
    await hass.async_block_till_done()
    assert len(pushes) == 2

    await base({**DOCKED, "errorCode": [{"identityCode": 0x01010013, "level": 2}]})
    await hass.async_block_till_done()
    state = hass.states.get(alerts_entity(hass))
    assert state.attributes["event_type"] == "error"
    assert state.attributes["codes"] == ["0x01010013"]


async def test_flapping_error_alerts_once(hass: HomeAssistant, client, base, pushes) -> None:
    washing_without_detergent = {**DOCKED, "errorCode": [{"identityCode": 0x01010072, "level": 1}]}
    for _ in range(3):
        await base(washing_without_detergent)
        await base(DOCKED)
    errors = [c for c in pushes if c.data["data"]["tag"] == "narwal_error"]
    assert len(errors) == 2
    assert errors[0].data["message"] == "The detergent compartment is empty. Refill it."


async def test_stranded_off_dock(hass: HomeAssistant, client, base, pushes) -> None:
    await base({"robotTaskStatus": {"task": "TASK_TYPE_IDLE", "idleStatus": "IDLE_STATUS_OUTSIDE_STATION"},
                        "batteryPercentage": 6.0})
    await hass.async_block_till_done()
    assert not pushes
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=21))
    await hass.async_block_till_done()
    state = hass.states.get(alerts_entity(hass))
    assert state.attributes["event_type"] == "stranded"
    assert state.attributes["message"].endswith("Battery is at 6%.")


async def test_options_choose_phones_and_alerts(hass: HomeAssistant, client, base, config_entry, pushes) -> None:
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_NOTIFY_TARGETS: ["mobile_app_phone_b"], CONF_PUSH_ALERTS: ["clean_water_tank"]},
    )
    assert result["type"] == "create_entry"

    await base(CLEANING)
    await hass.async_block_till_done()
    assert not pushes
    assert hass.states.get(alerts_entity(hass)).attributes["event_type"] == "clean_started"

    await base({**DOCKED, "cleanWaterTankState": "CLEAN_WATER_TANK_STATE_EMPTY"})
    await hass.async_block_till_done()
    assert [c.service for c in pushes] == ["mobile_app_phone_b"]
