"""Constants for the Narwal integration."""

from homeassistant.const import Platform

from .narwal_client.const import CleanMode, FanLevel, MopHumidity

DOMAIN = "narwal"

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Config entry data keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_PRODUCT_KEY = "product_key"
CONF_DEVICE_NAME = "device_name"
CONF_USER_UUID = "user_uuid"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"

# Legacy keys (kept for migration from manual config)
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_CLIENT_ID = "mqtt_client_id"

# Local broker: the robot's MQTT is redirected to a broker on the LAN.
CONF_CONNECTION = "connection"
CONNECTION_CLOUD = "cloud"
CONNECTION_LOCAL = "local"
CONF_BROKER_HOST = "broker_host"
CONF_BROKER_PORT = "broker_port"
CONF_BROKER_TLS = "broker_tls"
DEFAULT_LOCAL_PORT = 1883
# The robot accepts any account UUID in the request frame.
LOCAL_USER_UUID = "0" * 32

# Cached room list so start_clean works while the vacuum sleeps.
CONF_ROOMS_CACHE = "rooms_cache"

REGION_OPTIONS = {
    "us": "United States",
    "il": "Israel",
    "eu": "Europe",
    "cn": "China",
}

# Fan speed names shown in HA (order = app order).
FAN_SPEED_MAP: dict[str, FanLevel] = {
    "Quiet": FanLevel.QUIET,
    "Normal": FanLevel.NORMAL,
    "Strong": FanLevel.STRONG,
    "Max": FanLevel.MAX,
}
FAN_SPEED_LIST = list(FAN_SPEED_MAP)
FAN_SPEED_REVERSE = {v: k for k, v in FAN_SPEED_MAP.items()}

# Clean mode names are also used by the Lovelace card and send_command.
CLEAN_MODE_MAP: dict[str, CleanMode] = {
    "Vacuum & Mop": CleanMode.VACUUM_AND_MOP,
    "Vacuum then Mop": CleanMode.VACUUM_THEN_MOP,
    "Vacuum Only": CleanMode.VACUUM_ONLY,
    "Mop Only": CleanMode.MOP_ONLY,
}
CLEAN_MODE_LIST = list(CLEAN_MODE_MAP)
CLEAN_MODE_REVERSE = {v: k for k, v in CLEAN_MODE_MAP.items()}

MOP_HUMIDITY_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}
