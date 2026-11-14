"""Vacuum entity for Narwal robot vacuum."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .const import (
    CLEAN_MODE_MAP,
    COVERAGE_OPTIONS,
    FAN_SPEED_ALIASES,
    FAN_SPEED_LIST,
    FAN_SPEED_REVERSE,
    MOP_HUMIDITY_MAP,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import CleanMode, NarwalClient
from .narwal_client.models import (
    ACTIVITY_CLEANING,
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
    CleanPlan,
)

try:  # Home Assistant 2026.3+: native "clean area" (rooms mapped to HA areas)
    from homeassistant.components.vacuum import Segment

    _CLEAN_AREA = VacuumEntityFeature.CLEAN_AREA
except ImportError:  # pragma: no cover - older Home Assistant
    Segment = None  # type: ignore[assignment,misc]
    _CLEAN_AREA = VacuumEntityFeature(0)

_LOGGER = logging.getLogger(__name__)

_ACTIVITY = {
    ACTIVITY_CLEANING: VacuumActivity.CLEANING,
    ACTIVITY_DOCKED: VacuumActivity.DOCKED,
    ACTIVITY_IDLE: VacuumActivity.IDLE,
    ACTIVITY_PAUSED: VacuumActivity.PAUSED,
    ACTIVITY_RETURNING: VacuumActivity.RETURNING,
    ACTIVITY_ERROR: VacuumActivity.ERROR,
}

# vacuum.send_command commands that map straight onto a client method.
_SIMPLE_COMMANDS = {
    "wash_mop": "wash_mop",
    "dry_mop": "dry_mop",
    "wash_and_dry_mop": "wash_and_dry_mop",
    "empty_dustbin": "empty_dustbin",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NarwalVacuum(entry.runtime_data)])


class NarwalVacuum(NarwalEntity, StateVacuumEntity):
    """Representation of a Narwal robot vacuum."""

    _attr_translation_key = "narwal"
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.MAP
        | _CLEAN_AREA
    )
    _attr_fan_speed_list = FAN_SPEED_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "vacuum")
        self._segments_issue_raised = False

    @property
    def activity(self) -> VacuumActivity:
        return _ACTIVITY.get(self.state_data.activity, VacuumActivity.IDLE)

    @property
    def fan_speed(self) -> str | None:
        level = self.state_data.fan_level
        return FAN_SPEED_REVERSE.get(level) if level else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        attrs: dict[str, Any] = {
            "status": state.status_text,
            "rooms": {r.room_id: r.display_name for r in state.rooms},
            # Rooms marked no-go in the Narwal app; cleaning them is refused.
            "no_go_rooms": sorted(state.no_go_room_ids),
        }
        if state.current_room_id and state.is_cleaning:
            room = next((r for r in state.rooms if r.room_id == state.current_room_id), None)
            attrs["current_room"] = room.display_name if room else state.current_room_id
        if state.mop_humidity:
            attrs["mop_humidity"] = state.mop_humidity.name.lower()
        pose = state.robot_pose or (state.map.robot if state.map else None)
        if pose:
            attrs["robot_position"] = [round(pose.x), round(pose.y)]
        attrs["no_go_zones"] = [
            {"id": z.zone_id, "kind": z.kind, "points": [[round(x, 1), round(y, 1)] for x, y in z.polygon]}
            for z in state.no_go_zones
        ]
        return attrs

    # ---- basic control -------------------------------------------------

    async def async_start(self) -> None:
        client = self.coordinator.client
        if self.state_data.is_paused:
            await self._run(client.resume())
        else:
            await self._run(client.start_clean(plan=self.coordinator.clean_plan()))
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.stop())
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self._run(self.coordinator.client.pause())
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.return_to_base())
        await self.coordinator.async_request_refresh()

    async def async_locate(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.locate())

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        level = FAN_SPEED_ALIASES.get(fan_speed)
        if level is None:
            raise ServiceValidationError(f"Unknown fan speed {fan_speed!r}; use one of {FAN_SPEED_LIST}")
        await self._run(self.coordinator.client.set_fan_speed(level))
        self.async_write_ha_state()

    # ---- rooms ---------------------------------------------------------

    async def async_get_segments(self) -> list[Any]:
        """Rooms for Home Assistant's area mapping (vacuum.clean_area)."""
        state = self.state_data
        if not state.rooms:
            await self.coordinator.client.fetch_rooms()
        return [
            Segment(id=str(r.room_id), name=r.display_name)
            for r in self.state_data.rooms
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        room_ids = [int(s) for s in segment_ids]
        await self._clean_rooms(room_ids, self.coordinator.clean_plan())

    async def _clean_rooms(
        self, room_ids: list[int], plan: CleanPlan, room_plans: dict[int, CleanPlan] | None = None,
    ) -> None:
        known = {r.room_id for r in self.state_data.rooms}
        unknown = [r for r in room_ids if known and r not in known]
        if unknown:
            raise ServiceValidationError(f"Unknown room id(s) {unknown}; known rooms: {sorted(known)}")
        await self._run(self.coordinator.client.start_clean(room_ids=room_ids, plan=plan, room_plans=room_plans))
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._check_segments_changed()
        super()._handle_coordinator_update()

    @callback
    def _check_segments_changed(self) -> None:
        """Raise HA's repair issue when the robot's rooms change after mapping."""
        if Segment is None or self.registry_entry is None or self._segments_issue_raised:
            return
        last_seen = self.last_seen_segments
        rooms = self.state_data.rooms
        if last_seen is None or not rooms:
            return
        current = {(str(r.room_id), r.display_name) for r in rooms}
        if current != {(s.id, s.name) for s in last_seen}:
            self.async_create_segments_issue()
            self._segments_issue_raised = True

    # ---- send_command --------------------------------------------------

    async def async_send_command(
        self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any,
    ) -> None:
        """Extra commands.

        clean_rooms  params: rooms, cleaned in the order given: [ids], or
                     [{room: id, <settings>}] to set each room apart (the
                     app's Customize Rooms). Settings, all optional: mode,
                     fan_speed, mop_humidity, passes, mop_passes (Vacuum then
                     Mop), coverage: standard | intensive, stain_remover
        clean_zones  params: zones: [[x1, y1, x2, y2], ...] in map grid units
                     (see the robot_position attribute), plus the same
                     optional settings as clean_rooms
        set_room_order     params: rooms: [ids], the order a full clean follows
        add_no_go_zone     params: zone: [x1, y1, x2, y2] or points: [[x, y], ...],
                           kind: all | vacuum | mop
        add_barrier        params: start: [x, y], end: [x, y]
        remove_no_go_zone  params: id (see the no_go_zones attribute)
        rename_room        params: room, name ("" restores the room type's name)
        merge_rooms        params: rooms: [a, b]
        split_room         params: room, start: [x, y], end: [x, y]
        start_mapping      params: replace_current_map: true
        save_map
        manual_control     params: enabled: true | false
        drive              params: linear, angular (-100..100 percent)
        wash_mop | dry_mop | wash_and_dry_mop | empty_dustbin
        """
        if command in _MAP_COMMANDS:
            await self._run(_MAP_COMMANDS[command](self.coordinator.client, params if isinstance(params, dict) else {}))
            self.coordinator.async_map_edited()
            return
        if command in _CONTROL_COMMANDS:
            await self._run(_CONTROL_COMMANDS[command](self.coordinator.client, params if isinstance(params, dict) else {}))
            await self.coordinator.async_request_refresh()
            return
        p = params if isinstance(params, dict) else {}
        if command == "clean_rooms":
            rooms = p.get("rooms") or []
            if not rooms:
                raise ServiceValidationError("clean_rooms needs params.rooms (a list of room ids)")
            plan = self.coordinator.clean_plan(**_clean_settings(p))
            room_ids: list[int] = []
            room_plans: dict[int, CleanPlan] = {}
            for entry in rooms:
                if isinstance(entry, dict):
                    room_id = _room(entry)
                    room_plans[room_id] = replace(plan, **_clean_settings(entry))
                else:
                    room_id = int(entry)
                room_ids.append(room_id)
            if any(rp.mode == CleanMode.FREO_MIND for rp in room_plans.values()):
                raise ServiceValidationError("Freo Mind applies to the whole clean; set it as the top-level mode")
            await self._clean_rooms(room_ids, plan, room_plans)
        elif command == "clean_zones":
            zones = _parse_zones(p.get("zones"))
            await self._run(self.coordinator.client.start_zone_clean(
                zones, self.coordinator.clean_plan(**_clean_settings(p)),
            ))
            await self.coordinator.async_request_refresh()
        elif command == "set_room_order":
            order = [int(r) for r in p.get("rooms") or []]
            known = {r.room_id for r in self.state_data.rooms}
            if not order or set(order) - known:
                raise ServiceValidationError(f"set_room_order needs params.rooms from {sorted(known)}")
            await self._run(self.coordinator.client.set_room_order(order))
        elif command in _SIMPLE_COMMANDS:
            await self._run(getattr(self.coordinator.client, _SIMPLE_COMMANDS[command])())
        else:
            raise ServiceValidationError(
                f"Unknown command {command!r}; supported: clean_rooms, clean_zones, {', '.join(_SIMPLE_COMMANDS)}"
            )


def _clean_settings(p: dict[str, Any]) -> dict[str, Any]:
    """CleanPlan fields named in send_command params; absent ones stay None."""
    mode_name = p.get("mode")
    if mode_name is not None and mode_name not in CLEAN_MODE_MAP:
        raise ServiceValidationError(f"Unknown mode {mode_name!r}; use one of {list(CLEAN_MODE_MAP)}")
    fan_name = p.get("fan_speed")
    if fan_name is not None and fan_name not in FAN_SPEED_ALIASES:
        raise ServiceValidationError(f"Unknown fan_speed {fan_name!r}; use one of {FAN_SPEED_LIST}")
    mop_name = p.get("mop_humidity")
    if mop_name is not None and mop_name not in MOP_HUMIDITY_MAP:
        raise ServiceValidationError(f"Unknown mop_humidity {mop_name!r}; use dry, normal or wet")
    coverage = p.get("coverage")
    if coverage is not None and coverage not in COVERAGE_OPTIONS:
        raise ServiceValidationError(f"Unknown coverage {coverage!r}; use {' or '.join(COVERAGE_OPTIONS)}")
    passes = {key: _passes(p, key) for key in ("passes", "mop_passes")}
    if passes["mop_passes"] is None:
        passes["mop_passes"] = passes["passes"]
    return {
        "mode": CLEAN_MODE_MAP.get(mode_name) if mode_name else None,
        "fan_level": FAN_SPEED_ALIASES.get(fan_name) if fan_name else None,
        "mop_humidity": MOP_HUMIDITY_MAP.get(mop_name) if mop_name else None,
        **passes,
        "intensive": coverage == "intensive" if coverage else None,
        "stain_remover": bool(p["stain_remover"]) if "stain_remover" in p else None,
    }


def _passes(p: dict[str, Any], key: str) -> int | None:
    if p.get(key) is None:
        return None
    value = int(p[key])
    if not 1 <= value <= 3:
        raise ServiceValidationError(f"{key} must be 1, 2 or 3")
    return value


def _point(raw: Any, name: str) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ServiceValidationError(f"{name} must be [x, y]")
    try:
        return float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        raise ServiceValidationError(f"{name} must contain numbers") from None


def _room(p: dict[str, Any], key: str = "room") -> int:
    try:
        return int(p[key])
    except (KeyError, TypeError, ValueError):
        raise ServiceValidationError(f"params.{key} must be a room id") from None


def _no_go_polygon(p: dict[str, Any]) -> list[tuple[float, float]]:
    if "points" in p:
        points = p["points"]
        if not isinstance(points, list) or len(points) < 3:
            raise ServiceValidationError("params.points needs at least three [x, y] points")
        return [_point(pt, "Each point") for pt in points]
    x1, y1, x2, y2 = _parse_zones([p.get("zone")])[0]
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _start_mapping(client: NarwalClient, p: dict[str, Any]) -> Any:
    if p.get("replace_current_map") is not True:
        raise ServiceValidationError(
            "start_mapping replaces the current map, its rooms and no-go zones; "
            "pass params.replace_current_map: true to confirm"
        )
    return client.start_mapping(rebuild_current=True)


def _merge(client: NarwalClient, p: dict[str, Any]) -> Any:
    rooms = p.get("rooms")
    if not isinstance(rooms, list) or len(rooms) != 2:
        raise ServiceValidationError("merge_rooms needs params.rooms: [a, b]")
    return client.merge_rooms(int(rooms[0]), int(rooms[1]))


_MAP_COMMANDS: dict[str, Callable[[NarwalClient, dict[str, Any]], Awaitable[Any]]] = {
    "add_no_go_zone": lambda c, p: c.add_no_go_zone(_no_go_polygon(p), str(p.get("kind", "all"))),
    "add_barrier": lambda c, p: c.add_barrier(_point(p.get("start"), "start"), _point(p.get("end"), "end")),
    "remove_no_go_zone": lambda c, p: c.remove_no_go_zone(_room(p, "id")),
    "rename_room": lambda c, p: c.rename_room(_room(p), str(p.get("name", ""))),
    "merge_rooms": _merge,
    "split_room": lambda c, p: c.split_room(_room(p), _point(p.get("start"), "start"), _point(p.get("end"), "end")),
    "save_map": lambda c, p: c.save_map_update(),
}

_CONTROL_COMMANDS: dict[str, Callable[[NarwalClient, dict[str, Any]], Awaitable[Any]]] = {
    "start_mapping": _start_mapping,
    "manual_control": lambda c, p: c.set_manual_control(bool(p.get("enabled", True))),
    "drive": lambda c, p: c.drive(int(p.get("linear", 0)), int(p.get("angular", 0))),
}


def _parse_zones(raw: Any) -> list[tuple[int, int, int, int]]:
    if not isinstance(raw, list) or not raw:
        raise ServiceValidationError("clean_zones needs params.zones: a list of [x1, y1, x2, y2] rectangles")
    zones = []
    for zone in raw:
        if not isinstance(zone, (list, tuple)) or len(zone) != 4:
            raise ServiceValidationError(f"Zone {zone!r} must be [x1, y1, x2, y2]")
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in zone)
        except (TypeError, ValueError):
            raise ServiceValidationError(f"Zone {zone!r} must contain numbers") from None
        if x1 == x2 or y1 == y2:
            raise ServiceValidationError(f"Zone {zone!r} has no area")
        zones.append((x1, y1, x2, y2))
    return zones
