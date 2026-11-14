"""MQTT client for Narwal robot vacuum cloud communication."""

from __future__ import annotations

import asyncio
import logging
import math
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .const import (
    BROADCAST_TOPICS,
    CLEAN_MODE_LABEL,
    COMMAND_RESPONSE_TIMEOUT,
    MQTT_BROKER,
    MQTT_PORT,
    SERVICE_RESULT_MESSAGES,
    TOPIC_CMD_ACTIVE_ROBOT,
    TOPIC_CMD_DRY_MOP,
    TOPIC_CMD_DUST_GATHERING,
    TOPIC_CMD_FORCE_END,
    TOPIC_CMD_GET_BASE_STATUS,
    TOPIC_CMD_GET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE,
    TOPIC_CMD_GET_DEVICE_INFO,
    TOPIC_CMD_GET_FEATURE_LIST,
    TOPIC_CMD_GET_FIRMWARE_VERSION,
    TOPIC_CMD_BUILD_MAP,
    TOPIC_CMD_GET_EDITABLE_MAP,
    TOPIC_CMD_GET_MAP,
    TOPIC_CMD_MAP_OPERATION,
    TOPIC_CMD_SAVE_MAP_UPDATE,
    TOPIC_CMD_SET_MANUAL_CONTROL,
    TOPIC_CMD_VELOCITY_CONTROL,
    TOPIC_CMD_PAUSE,
    TOPIC_CMD_RECALL,
    TOPIC_CMD_RESET_CONSUMABLE,
    TOPIC_CMD_RESUME,
    TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_SET_FAN_LEVEL,
    TOPIC_CMD_SET_MOP_HUMIDITY,
    TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_WASH_AND_DRY_MOP,
    TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_YELL,
    TOPIC_DISPLAY_MAP,
    TOPIC_ROBOT_BASE_STATUS,
    TOPIC_UPGRADE_STATUS,
    TOPIC_WORKING_STATUS,
    CleanMode,
    FanLevel,
    MopHumidity,
)
from .models import CommandResponse, MapSnapshot, NarwalState, NoGoZone, Pose, strip_narwal_frame
from .proto import ProtoError, decode, encode, encode_varint

_LOGGER = logging.getLogger(__name__)

# The robot ignored a 0.6 x 0.5 m zone and cleaned a 1.2 x 1.2 m one.
MIN_ZONE_SIDE_MM = 960

_ZONE_TASK_TYPES = {
    CleanMode.VACUUM_ONLY: "TASK_TYPE_GENERAL_SWEEP",
    CleanMode.MOP_ONLY: "TASK_TYPE_GENERAL_MOP",
    CleanMode.VACUUM_THEN_MOP: "TASK_TYPE_GENERAL_SWEEP_THEN_MOP",
    CleanMode.VACUUM_AND_MOP: "TASK_TYPE_GENERAL_SWEEP_MOP_SYNC",
}


def _coverage_path(left: int, bottom: int, right: int, top: int, step: int = 2) -> list[dict[str, int]]:
    """A serpentine walk over the rectangle's interior cells."""
    points: list[dict[str, int]] = []
    forward = True
    for y in range(bottom + 1, top, step):
        xs = range(left + 1, right) if forward else range(right - 1, left, -1)
        points.extend({"x": x, "y": y} for x in xs)
        forward = not forward
    return points or [{"x": (left + right) // 2, "y": (bottom + top) // 2}]


_NO_GO_TYPES = {
    "all": "FORBIDDEN_TYPE_SWEEP_MOP",
    "vacuum": "FORBIDDEN_TYPE_SWEEP",
    "mop": "FORBIDDEN_TYPE_MOP",
}

# Response message type for each command we decode.
RESPONSE_TYPES: dict[str, str] = {
    TOPIC_CMD_GET_BASE_STATUS: "GetDeviceBaseStatus_Response",
    TOPIC_CMD_GET_CONFIG: "GetConfig_Response",
    TOPIC_CMD_SET_CONFIG: "SetConfig_Response",
    TOPIC_CMD_GET_CONSUMABLE: "GetConsumableInfo_Response",
    TOPIC_CMD_GET_FEATURE_LIST: "GetFeatureList_Response",
    TOPIC_CMD_GET_FIRMWARE_VERSION: "GetFirmwareVersion_Response",
    TOPIC_CMD_GET_MAP: "GetMap_Response",
    TOPIC_CMD_SET_FAN_LEVEL: "SetFanLevel_Response",
    TOPIC_CMD_SET_MOP_HUMIDITY: "SetMopHumidity_Response",
    TOPIC_CMD_START_CLEAN: "StartClean_Response",
}
# Commands whose response just carries a ServiceResult in field 1.
_GENERIC_RESULT = "SetFanLevel_Response"

# Response topics subscribed on connect. Narwal's broker only routes to
# explicit subscriptions, so pre-subscribing saves a SUBACK round trip per
# command (previously a fixed 0.5 s sleep on every call).
_PRESUBSCRIBE = (
    TOPIC_CMD_GET_BASE_STATUS, TOPIC_CMD_GET_CONFIG, TOPIC_CMD_SET_CONFIG,
    TOPIC_CMD_GET_CONSUMABLE, TOPIC_CMD_GET_MAP, TOPIC_CMD_YELL, TOPIC_CMD_PAUSE,
    TOPIC_CMD_RESUME, TOPIC_CMD_FORCE_END, TOPIC_CMD_RECALL, TOPIC_CMD_START_CLEAN,
    TOPIC_CMD_SET_FAN_LEVEL, TOPIC_CMD_SET_MOP_HUMIDITY, TOPIC_CMD_WASH_MOP,
    TOPIC_CMD_DRY_MOP, TOPIC_CMD_WASH_AND_DRY_MOP, TOPIC_CMD_DUST_GATHERING,
)

_PUSH_TYPES = {
    TOPIC_ROBOT_BASE_STATUS: "RobotBaseStatus",
    TOPIC_WORKING_STATUS: "WorkingStatus",
    TOPIC_DISPLAY_MAP: "DisplayMap",
    TOPIC_UPGRADE_STATUS: "OTAUpgradeStatus",
}


class NarwalConnectionError(Exception):
    """Raised when connection to the vacuum fails."""


class NarwalBrokerAuthError(NarwalConnectionError):
    """Raised when the MQTT broker rejects the credentials."""


class NarwalCommandError(Exception):
    """Raised when a command fails, is rejected, or times out."""

    def __init__(self, message: str, *, timeout: bool = False, result: Any = None) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.result = result


def _pb_string(field_num: int, value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data


def _pb_varint(field_num: int, value: int) -> bytes:
    return encode_varint(field_num << 3) + encode_varint(value)


class NarwalClient:
    """MQTT client for Narwal vacuum cloud communication."""

    def __init__(
        self,
        product_key: str,
        device_name: str,
        user_uuid: str,
        mqtt_username: str | None,
        mqtt_password: str | None,
        mqtt_client_id: str | None = None,
        broker: str = MQTT_BROKER,
        port: int = MQTT_PORT,
        use_tls: bool = True,
    ) -> None:
        self.product_key = product_key
        self.device_name = device_name
        self.user_uuid = user_uuid
        self.broker = broker
        self.port = port
        self.use_tls = use_tls

        self.state = NarwalState()
        self.on_state_update: Callable[[NarwalState], None] | None = None

        self._mqtt_username = mqtt_username
        self._mqtt_password = mqtt_password
        self._mqtt_client_id_prefix = mqtt_client_id or f"app_{user_uuid}"
        self._mqtt_client_id = f"{self._mqtt_client_id_prefix}_{uuid.uuid4()}"

        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._connect_settled = threading.Event()
        self._connect_rejection: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_responses: dict[str, tuple[threading.Event, list[bytes | None]]] = {}
        self._pending_subacks: dict[int, threading.Event] = {}
        self._subscribed: set[str] = set()
        self._sub_lock = threading.Lock()
        self._command_locks: dict[str, threading.Lock] = {}
        self.last_message_time: float = 0.0

    @property
    def base_topic(self) -> str:
        return f"/{self.product_key}/{self.device_name}"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Framing
    # ------------------------------------------------------------------

    def _auth_frame(self) -> bytes:
        """0x01 + varint(len) + {1: uuid, 2: uuid}; every request starts with it."""
        inner = _pb_string(1, self.user_uuid) + _pb_string(2, self.user_uuid)
        return b"\x01" + encode_varint(len(inner)) + inner

    def _build_publish_properties(self, topic: str) -> Properties:
        corr = (
            _pb_string(1, str(uuid.uuid1()))
            + _pb_varint(3, 0)
            + _pb_varint(4, int(time.time() * 1000))
        )
        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = f"{topic}/response"
        props.CorrelationData = corr
        return props

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the MQTT broker (blocking parts run in an executor)."""
        self._loop = asyncio.get_running_loop()
        self._connected.clear()
        self._connect_settled.clear()
        self._connect_rejection = None

        def _connect_blocking() -> None:
            self._setup_mqtt_client()
            settled = self._connect_settled.wait(timeout=15.0)
            if self._connected.is_set():
                return
            if self._client:
                self._client.loop_stop()
            if settled and self._connect_rejection:
                raise NarwalBrokerAuthError(f"MQTT broker rejected the connection: {self._connect_rejection}")
            raise NarwalConnectionError("MQTT connection timed out")

        await self._loop.run_in_executor(None, _connect_blocking)
        await self.notify_active()

    def _setup_mqtt_client(self) -> None:
        self._mqtt_client_id = f"{self._mqtt_client_id_prefix}_{uuid.uuid4()}"
        _LOGGER.info("Connecting to %s:%d, base_topic=%s", self.broker, self.port, self.base_topic)
        self._subscribed.clear()
        self._client = mqtt.Client(
            client_id=self._mqtt_client_id,
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if self._mqtt_username:
            self._client.username_pw_set(self._mqtt_username, self._mqtt_password or None)
        if self.use_tls:
            self._client.tls_set_context(ssl.create_default_context())
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.connect(self.broker, self.port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        if not (str(reason_code) == "Success" or reason_code == 0):
            _LOGGER.error("MQTT connection rejected: %s", reason_code)
            self._connect_rejection = str(reason_code)
            self._connect_settled.set()
            return
        _LOGGER.info("MQTT connected (%s)", self.base_topic)
        self._subscribed.clear()
        topics = [f"{self.base_topic}/{t}" for t in BROADCAST_TOPICS]
        topics += [f"{self.base_topic}/{t}/response" for t in _PRESUBSCRIBE]
        for topic in topics:
            client.subscribe(topic, qos=1)
            self._subscribed.add(topic)
        self._connected.set()
        self._connect_settled.set()
        self.state.device_reachable = True
        self._notify_state_update()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        evt = self._pending_subacks.pop(mid, None)
        if evt:
            evt.set()

    def _on_disconnect(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        self._connected.clear()
        self.state.device_reachable = False
        self._notify_state_update()

    def _notify_state_update(self) -> None:
        cb = self.on_state_update
        loop = self._loop
        if cb and loop:
            try:
                loop.call_soon_threadsafe(cb, self.state)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Incoming messages
    # ------------------------------------------------------------------

    def _on_message(self, client, userdata, msg):
        self.last_message_time = time.time()
        pending = self._pending_responses.pop(msg.topic, None)
        if pending:
            event, holder = pending
            holder[0] = msg.payload
            event.set()
            return

        suffix = msg.topic[len(self.base_topic):].lstrip("/")
        msg_type = _PUSH_TYPES.get(suffix)
        if not msg_type:
            return
        try:
            data = decode(msg_type, strip_narwal_frame(msg.payload))
        except ProtoError:
            _LOGGER.debug("Undecodable %s push: %s", suffix, msg.payload.hex())
            return
        if suffix == TOPIC_ROBOT_BASE_STATUS:
            self.state.update_base_status(data)
        elif suffix == TOPIC_WORKING_STATUS:
            self.state.update_working_status(data)
        elif suffix == TOPIC_DISPLAY_MAP:
            self.state.update_display_map(data)
        elif suffix == TOPIC_UPGRADE_STATUS:
            _LOGGER.info("Firmware upgrade status: %s", data)
        self.state.device_reachable = True
        self._notify_state_update()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send_command(
        self,
        command: str,
        request: bytes = b"",
        *,
        timeout: float = COMMAND_RESPONSE_TIMEOUT,
        response_type: str | None = None,
        check: bool = True,
        retry: bool = True,
    ) -> CommandResponse:
        """Send ``command`` with an encoded request body; wait for the reply.

        On timeout the robot is usually in deep sleep: re-announce ourselves
        (which wakes it) and retry once. With ``check`` a non-success
        ServiceResult raises NarwalCommandError with a readable reason.
        """
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")
        loop = self._loop or asyncio.get_running_loop()
        # response_type=None -> look up / generic ServiceResult; "" -> don't decode.
        rtype = RESPONSE_TYPES.get(command, _GENERIC_RESULT) if response_type is None else response_type
        try:
            raw = await loop.run_in_executor(None, self._send_blocking, command, request, timeout)
        except NarwalCommandError as err:
            if not (retry and err.timeout):
                raise
            _LOGGER.info("%s timed out; waking robot and retrying once", command)
            await self.notify_active()
            await asyncio.sleep(1.5)
            raw = await loop.run_in_executor(None, self._send_blocking, command, request, timeout)

        resp = CommandResponse.parse(command, raw, rtype)
        if check and not resp.success:
            reason = SERVICE_RESULT_MESSAGES.get(str(resp.result), str(resp.result))
            raise NarwalCommandError(f"{command}: {reason}", result=resp.result)
        return resp

    def _ensure_subscribed(self, topic: str) -> None:
        with self._sub_lock:
            if topic in self._subscribed:
                return
            evt = threading.Event()
            rc, mid = self._client.subscribe(topic, qos=1)
            if rc == 0:
                self._pending_subacks[mid] = evt
                evt.wait(timeout=3.0)
                self._subscribed.add(topic)

    def _send_blocking(self, command: str, request: bytes, timeout: float) -> bytes:
        lock = self._command_locks.setdefault(command, threading.Lock())
        with lock:
            topic = f"{self.base_topic}/{command}"
            response_topic = f"{topic}/response"
            self._ensure_subscribed(response_topic)
            event = threading.Event()
            holder: list[bytes | None] = [None]
            self._pending_responses[response_topic] = (event, holder)
            self._client.publish(
                topic, self._auth_frame() + request, qos=1,
                properties=self._build_publish_properties(topic),
            )
            if not event.wait(timeout=timeout):
                self._pending_responses.pop(response_topic, None)
                raise NarwalCommandError(f"{command} timed out", timeout=True)
            return holder[0] or b""

    async def _publish_no_response(self, command: str, request: bytes) -> None:
        if not self._client or not self.connected:
            raise NarwalConnectionError("Not connected")
        topic = f"{self.base_topic}/{command}"
        self._client.publish(
            topic, self._auth_frame() + request, qos=1,
            properties=self._build_publish_properties(topic),
        )

    async def notify_active(self) -> None:
        """Announce an app client; the robot then pushes status every ~2 s."""
        try:
            request = encode("ActiveRobotPublish_Request", {
                "topicsDuration": {
                    "workStatusDuration": 2000,
                    "displayMapDuration": 2000,
                    "robotBaseStatusDuration": 2000,
                },
                "publishDuration": 60000,
            })
            await self._publish_no_response(TOPIC_CMD_ACTIVE_ROBOT, request)
        except Exception:  # noqa: BLE001 - best effort keepalive
            _LOGGER.debug("active_robot_publish failed", exc_info=True)

    # ---- status / info -------------------------------------------------

    async def request_status_update(self) -> None:
        resp = await self.send_command(TOPIC_CMD_GET_BASE_STATUS, retry=False)
        base = resp.message.get("robotBaseStatus")
        if isinstance(base, dict):
            self.state.update_base_status(base)
            self.state.device_reachable = True
            self._notify_state_update()

    async def fetch_config(self) -> dict[str, Any]:
        resp = await self.send_command(TOPIC_CMD_GET_CONFIG)
        config = resp.message.get("config")
        if isinstance(config, dict):
            self.state.config = config
            self._notify_state_update()
        return self.state.config

    async def fetch_consumables(self) -> dict[str, list[str]]:
        resp = await self.send_command(TOPIC_CMD_GET_CONSUMABLE, check=False)
        info = resp.message.get("consumableInfo") or {}
        self.state.consumables_due = {
            "maintain": [i for i in info.get("maintainItems", []) if isinstance(i, str)],
            "replace": [i for i in info.get("replaceItems", []) if isinstance(i, str)],
        }
        self._notify_state_update()
        return self.state.consumables_due

    async def fetch_device_info(self) -> None:
        """Feature flags and firmware version (static; fetch once)."""
        try:
            resp = await self.send_command(TOPIC_CMD_GET_FEATURE_LIST, check=False)
            self.state.features = {k: v for k, v in resp.message.items() if not k.startswith("#")}
        except NarwalCommandError:
            _LOGGER.debug("get_feature_list failed")
        try:
            resp = await self.send_command(TOPIC_CMD_GET_FIRMWARE_VERSION, check=False)
            text = resp.message.get("firmwareVersionText")
            if not text:
                resp = await self.send_command(TOPIC_CMD_GET_DEVICE_INFO, check=False, response_type="")
                text = _device_info_version(resp.raw)
            self.state.firmware_version = (text or "").strip()
        except NarwalCommandError:
            _LOGGER.debug("firmware version query failed")

    async def get_map(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_GET_MAP, timeout=20.0)

    async def fetch_map(self) -> MapSnapshot | None:
        """Fetch the static map; updates rooms and map geometry in state."""
        resp = await self.get_map()
        static = resp.message.get("map")
        if not isinstance(static, dict):
            return None
        return self._apply_static_map(static)

    def _apply_static_map(self, static: dict[str, Any]) -> MapSnapshot | None:
        self.state.update_rooms_from_map(static)
        border = static.get("border") or {}
        grid = static.get("mapData") or b""
        if not grid or not static.get("width"):
            return self.state.map
        snapshot = MapSnapshot(
            map_id=int(static.get("mapId", 0) or 0),
            width=int(static["width"]),
            height=int(static.get("height", 0)),
            resolution_mm=int(static.get("resolution", 60) or 60),
            left=int(border.get("left", 0)),
            bottom=int(border.get("bottom", 0)),
            grid=grid,
            rotate_angle=int(static.get("rotateAngle", 0) or 0),
            station=Pose.from_msg(static.get("station")),
            robot=Pose.from_msg(static.get("robotPose")),
        )
        self.state.map = snapshot
        if snapshot.robot and not self.state.robot_pose:
            self.state.robot_pose = snapshot.robot
        self._notify_state_update()
        return snapshot

    async def fetch_rooms(self) -> None:
        try:
            await self.fetch_map()
        except NarwalCommandError:
            _LOGGER.warning("Failed to fetch rooms from map")

    # ---- control -------------------------------------------------------

    async def locate(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_YELL)

    async def pause(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_PAUSE)

    async def resume(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RESUME)

    async def stop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_FORCE_END)

    async def return_to_base(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_RECALL)

    async def wash_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_WASH_MOP)

    async def dry_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_DRY_MOP)

    async def wash_and_dry_mop(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_WASH_AND_DRY_MOP)

    async def empty_dustbin(self) -> CommandResponse:
        return await self.send_command(TOPIC_CMD_DUST_GATHERING)

    async def set_fan_speed(self, level: FanLevel) -> CommandResponse | None:
        """Change suction. The robot only accepts this during a clean, so
        while docked the choice is remembered and sent with the next clean."""
        self.state.preferred_fan_level = level
        resp = None
        if self.state.is_cleaning:
            request = encode("SetFanLevel_Request", {
                "level": level.value, "operationType": "OPERATION_TYPE_SET_VALUE",
            })
            resp = await self.send_command(TOPIC_CMD_SET_FAN_LEVEL, request)
            self.state.base["fanLevel"] = {v: k for k, v in _FAN_LABELS.items()}[level]
        self._notify_state_update()
        return resp

    async def set_mop_humidity(self, level: MopHumidity) -> CommandResponse | None:
        """Change mop wetness; same docked/cleaning behaviour as suction."""
        self.state.preferred_mop_humidity = level
        resp = None
        if self.state.is_cleaning:
            request = encode("SetMopHumidity_Request", {
                "mopHumidity": level.value, "operationType": "OPERATION_TYPE_SET_VALUE",
            })
            resp = await self.send_command(TOPIC_CMD_SET_MOP_HUMIDITY, request)
            self.state.base["mopHumidity"] = f"MOP_HUMIDITY_{level.name}"
        self._notify_state_update()
        return resp

    async def set_config(self, **values: Any) -> CommandResponse:
        """Change robot settings, e.g. ``set_config(childLockEnabled=True)``.

        Field names are SetConfig_Request's; on success the cached GetConfig
        values are refreshed from the robot.
        """
        request = encode("SetConfig_Request", values)
        resp = await self.send_command(TOPIC_CMD_SET_CONFIG, request)
        try:
            await self.fetch_config()
        except NarwalCommandError:
            self.state.config.update(values)
            self._notify_state_update()
        return resp

    async def reset_consumable(self, replace_item: str) -> CommandResponse:
        request = encode("ResetConsumableInfo_Request", {
            "consumableInfo": {"replaceItems": [replace_item]},
        })
        resp = await self.send_command(
            TOPIC_CMD_RESET_CONSUMABLE, request,
            response_type="ResetConsumableInfo_Response", check=False,
        )
        await self.fetch_consumables()
        return resp

    def build_clean_request(
        self,
        room_ids: list[int],
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> bytes:
        """Encode a StartClean_Request: one CleanItem per room, in order."""
        zones = [{"zoneType": "ZONE_TYPE_ROOM", "zoneId": room_id} for room_id in room_ids]
        return self._build_start_clean(zones, mode, fan_level, mop_humidity, passes)

    def build_zone_clean_request(
        self,
        zones: list[tuple[int, int, int, int]],
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> bytes:
        """Encode rectangles (x1, y1, x2, y2) in map grid units; cleaningBounds is the path driven."""
        options = []
        for zone_id, (x1, y1, x2, y2) in enumerate(zones, start=1):
            left, right = sorted((int(x1), int(x2)))
            bottom, top = sorted((int(y1), int(y2)))
            options.append({
                "zoneType": "ZONE_TYPE_ZONE",
                "zoneId": zone_id,
                "selectionBounds": [
                    {"x": left, "y": top},
                    {"x": right, "y": top},
                    {"x": right, "y": bottom},
                    {"x": left, "y": bottom},
                ],
                "cleaningBounds": _coverage_path(left, bottom, right, top),
            })
        return self._build_start_clean(
            options, mode, fan_level, mop_humidity, passes,
            task_type=_ZONE_TASK_TYPES[mode],
            option={"enableSmartMode": True},
            param_extra={"overlapLevel": "OVERLAP_LEVEL_NORMAL", "enableSmartMode": True},
        )

    def _build_start_clean(
        self,
        zones: list[dict[str, Any]],
        mode: CleanMode,
        fan_level: FanLevel | None,
        mop_humidity: MopHumidity | None,
        passes: int,
        task_type: str = "TASK_TYPE_CUSTOM_CLEAN",
        option: dict[str, Any] | None = None,
        param_extra: dict[str, Any] | None = None,
    ) -> bytes:
        fan = fan_level or self.state.fan_level or FanLevel.NORMAL
        mop = mop_humidity or self.state.mop_humidity or MopHumidity.NORMAL
        vacuum = mode in (CleanMode.VACUUM_ONLY, CleanMode.VACUUM_AND_MOP, CleanMode.VACUUM_THEN_MOP)
        mopping = mode in (CleanMode.MOP_ONLY, CleanMode.VACUUM_AND_MOP, CleanMode.VACUUM_THEN_MOP)
        # Passes go in the field matching the mode (CleanTask.CleanParam).
        times: dict[str, int] = {
            CleanMode.VACUUM_ONLY: {"sweepTime": passes},
            CleanMode.MOP_ONLY: {"mopTime": passes},
            CleanMode.VACUUM_AND_MOP: {"sweepMopSyncTime": passes},
            CleanMode.VACUUM_THEN_MOP: {"sweepTime": passes, "mopTime": passes},
        }[mode]
        items = []
        for order, zone in enumerate(zones, start=1):
            items.append({
                "zone": zone,
                "param": {
                    "mode": CLEAN_MODE_LABEL[mode],
                    "fanLevel": fan.value if vacuum else None,
                    "mopHumidity": mop.value if mopping else None,
                    **times,
                    **(param_extra or {}),
                },
                "order": order,
            })
        task: dict[str, Any] = {"items": items, "taskType": task_type}
        if option:
            task["option"] = option
        if self.state.map and self.state.map.map_id:
            task["mapId"] = self.state.map.map_id
        return encode("StartClean_Request", {"task": task})

    async def start_clean(
        self,
        room_ids: list[int] | None = None,
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> CommandResponse:
        """Start cleaning the given rooms (all known rooms if none given)."""
        await self.notify_active()
        if not room_ids:
            if not self.state.rooms or self.state.map is None:
                await self.fetch_rooms()
            room_ids = [
                r.room_id for r in self.state.rooms
                if r.room_id not in self.state.no_go_room_ids
            ]
        if not room_ids:
            raise NarwalCommandError("Cannot start: no rooms known yet (map not loaded)")
        request = self.build_clean_request(room_ids, mode, fan_level, mop_humidity, passes)
        return await self.send_command(TOPIC_CMD_START_CLEAN, request)

    async def start_zone_clean(
        self,
        zones: list[tuple[int, int, int, int]],
        mode: CleanMode = CleanMode.VACUUM_AND_MOP,
        fan_level: FanLevel | None = None,
        mop_humidity: MopHumidity | None = None,
        passes: int = 1,
    ) -> CommandResponse:
        """Clean rectangles (x1, y1, x2, y2) given in the map's grid units."""
        if not zones:
            raise NarwalCommandError("Cannot start: no zones given")
        await self.notify_active()
        if self.state.map is None:
            await self.fetch_map()
        resolution = self.state.map.resolution_mm if self.state.map else 60
        min_side = math.ceil(MIN_ZONE_SIDE_MM / resolution)
        for x1, y1, x2, y2 in zones:
            if abs(x2 - x1) < min_side or abs(y2 - y1) < min_side:
                raise NarwalCommandError(
                    f"Zone {(x1, y1, x2, y2)} is too small: the robot ignores zones under "
                    f"{min_side} grid cells (about {MIN_ZONE_SIDE_MM / 1000:.1f} m) per side"
                )
        request = self.build_zone_clean_request(zones, mode, fan_level, mop_humidity, passes)
        return await self.send_command(TOPIC_CMD_START_CLEAN, request)

    # ------------------------------------------------------------------
    # Map editing (map/operation applies edits against the current edit version)
    # ------------------------------------------------------------------

    async def get_editable_map(self) -> dict[str, Any]:
        """The map the robot accepts edits against, with its edit version."""
        if self.state.map is None:
            await self.fetch_map()
        map_id = self.state.map.map_id if self.state.map else 0
        resp = await self.send_command(
            TOPIC_CMD_GET_EDITABLE_MAP, encode("GetEditableMap_Request", {"mapId": map_id}),
            response_type="GetEditableMap_Response", timeout=15.0,
        )
        return resp.message

    async def edit_map(self, operations: list[dict[str, Any]]) -> CommandResponse:
        """Apply MapOperationPayload operations; the robot returns the updated map."""
        editable = await self.get_editable_map()
        static = editable.get("map") or {}
        request = encode("MapOperation_Request", {
            "mapId": static.get("mapId") or (self.state.map.map_id if self.state.map else 0),
            "mapVersion": static.get("mapVersion", 0),
            "editVersion": editable.get("editVersion", 0),
            "operations": operations,
        })
        resp = await self.send_command(
            TOPIC_CMD_MAP_OPERATION, request, response_type="MapOperation_Response", timeout=15.0,
        )
        updated = resp.message.get("map")
        if isinstance(updated, dict):
            self._apply_static_map(updated)
        return resp

    async def add_no_go_zone(self, polygon: list[tuple[float, float]], kind: str = "all") -> NoGoZone:
        """Add a no-go region (polygon in map grid units); kind is all, vacuum or mop."""
        if len(polygon) < 3:
            raise NarwalCommandError("A no-go zone needs at least three points")
        if kind not in _NO_GO_TYPES:
            raise NarwalCommandError(f"Unknown no-go kind {kind!r}; use all, vacuum or mop")
        if self.state.map is None:
            await self.fetch_map()
        zone_id = max((z.zone_id for z in self.state.no_go_zones), default=0) + 1
        await self.edit_map([{"forbiddenAreaOperation": {
            "operation": "OPERATION_SET",
            "forbidden": {
                "polygon": [{"x": float(x), "y": float(y)} for x, y in polygon],
                "type": _NO_GO_TYPES[kind],
                "id": zone_id,
                "mode": "FORBIDDEN_MODE_REGION",
            },
        }}])
        return NoGoZone(zone_id, [(float(x), float(y)) for x, y in polygon], kind)

    async def add_barrier(
        self, start: tuple[float, float], end: tuple[float, float], width: float = 1.0
    ) -> NoGoZone:
        """A virtual wall as a thin no-go strip; the CX7 has no native virtual walls."""
        (x1, y1), (x2, y2) = start, end
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1:
            raise NarwalCommandError("A barrier must be at least one grid cell long")
        nx, ny = -(y2 - y1) / length * width / 2, (x2 - x1) / length * width / 2
        polygon = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
        return await self.add_no_go_zone(polygon, "all")

    async def remove_no_go_zone(self, zone_id: int) -> CommandResponse:
        return await self.edit_map([{"forbiddenAreaOperation": {
            "operation": "OPERATION_DELETE", "forbidden": {"id": int(zone_id)},
        }}])

    async def rename_room(self, room_id: int, name: str) -> CommandResponse:
        """Set a room's custom name; an empty name falls back to the room type's name."""
        editable = await self.get_editable_map()
        info = next(
            (r for r in (editable.get("map") or {}).get("roomInfos", []) if r.get("roomId") == room_id),
            None,
        )
        if info is None:
            raise NarwalCommandError(f"Unknown room id {room_id}")
        return await self.edit_map([{"roomInfoOperation": {"roomInfo": {**info, "customName": name}}}])

    async def merge_rooms(self, room_a: int, room_b: int) -> CommandResponse:
        return await self.edit_map([{"roomMergeOperation": {"roomId1": int(room_a), "roomId2": int(room_b)}}])

    async def split_room(
        self, room_id: int, start: tuple[float, float], end: tuple[float, float]
    ) -> CommandResponse:
        """Split a room along a line given in map grid units."""
        return await self.edit_map([{"roomSegOperation": {
            "startSegPoint": {"x": round(start[0]), "y": round(start[1])},
            "endSegPoint": {"x": round(end[0]), "y": round(end[1])},
            "leftRoomId": int(room_id),
        }}])

    # ------------------------------------------------------------------
    # Mapping and manual control
    # ------------------------------------------------------------------

    async def start_mapping(self, rebuild_current: bool = True) -> CommandResponse:
        """Explore and build the main map; with rebuild_current the existing map is replaced."""
        await self.notify_active()
        request = encode("BuildMap_Request", {
            "buildType": "MAP_BUILD_TYPE_MAIN_MAP", "isRebuildCurrentMap": rebuild_current,
        })
        return await self.send_command(TOPIC_CMD_BUILD_MAP, request, response_type="BuildMap_Response")

    async def save_map_update(self) -> CommandResponse:
        resp = await self.send_command(
            TOPIC_CMD_SAVE_MAP_UPDATE, b"", response_type="SaveMapUpdate_Response", check=False,
        )
        await self.fetch_map()
        return resp

    async def set_manual_control(self, enabled: bool) -> CommandResponse:
        mode = "MANUAL_CONTROL_MODE_JOYSTICK" if enabled else "MANUAL_CONTROL_MODE_OFF"
        return await self.send_command(
            TOPIC_CMD_SET_MANUAL_CONTROL,
            encode("SetManualControlMode_Request", {"manualControlMode": mode}),
            response_type="SetManualControlMode_Response",
        )

    async def drive(self, linear: int, angular: int) -> None:
        """Joystick step: linear and angular velocity as -100..100 percent."""
        await self._publish_no_response(TOPIC_CMD_VELOCITY_CONTROL, encode("TelecontrolVelocity", {
            "linearVelocity": max(-100, min(100, int(linear))),
            "angularVelocity": max(-100, min(100, int(angular))),
            "type": "VELOCITY_TYPE_PERCENTAGE",
        }))

    # ------------------------------------------------------------------
    # Teardown / discovery
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Disconnect without flipping availability (planned reconnect/unload)."""
        client = self._client
        self._client = None
        self._connected.clear()
        if client:
            client.on_connect = None
            client.on_message = None
            client.on_disconnect = None
            client.on_subscribe = None
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._stop_mqtt_client, client), timeout=5.0,
                )
            except TimeoutError:
                _LOGGER.warning("MQTT disconnect timed out — forcing cleanup")
                try:
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _stop_mqtt_client(client: mqtt.Client) -> None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        client.loop_stop()

    @staticmethod
    def discover_devices_via_mqtt(
        product_key: str,
        user_uuid: str,
        mqtt_password: str,
        broker: str,
        port: int = MQTT_PORT,
        timeout: float = 10.0,
    ) -> list[str]:
        """Listen on /{product_key}/+/# and return device names seen (blocking)."""
        discovered: list[str] = []
        found_event = threading.Event()
        client = mqtt.Client(
            client_id=f"app_{user_uuid}_{uuid.uuid4()}",
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.username_pw_set(user_uuid, mqtt_password)
        client.tls_set_context(ssl.create_default_context())

        def on_connect(cl, ud, flags, rc, props=None):
            if str(rc) == "Success" or rc == 0:
                cl.subscribe(f"/{product_key}/+/#", qos=1)

        def on_message(cl, ud, msg):
            parts = msg.topic.split("/")
            if len(parts) >= 3 and parts[1] == product_key and parts[2] not in discovered:
                discovered.append(parts[2])
                found_event.set()

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(broker, port, keepalive=30)
            client.loop_start()
            found_event.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("MQTT discovery failed", exc_info=True)
        finally:
            client.loop_stop()
            client.disconnect()
        return discovered


_FAN_LABELS = {
    "FAN_LEVEL_MUTE": FanLevel.QUIET,
    "FAN_LEVEL_NORMAL": FanLevel.NORMAL,
    "FAN_LEVEL_STRONG": FanLevel.STRONG,
    "FAN_LEVEL_DEEP": FanLevel.DEEP,
    "FAN_LEVEL_SUPER": FanLevel.MAX,
}


def _device_info_version(body: bytes) -> str:
    """common/get_device_info reply: field 3 holds the firmware text."""
    from .proto import iter_fields

    try:
        for num, wire, value in iter_fields(body):
            if num == 3 and wire == 2:
                return value.decode(errors="replace")
    except ProtoError:
        pass
    return ""
