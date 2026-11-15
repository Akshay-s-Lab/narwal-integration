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
    Platform.EVENT,
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

# Fan speed names shown in HA, in the app's order and wording.
FAN_SPEED_MAP: dict[str, FanLevel] = {
    "Quiet": FanLevel.QUIET,
    "Normal": FanLevel.NORMAL,
    "Turbo": FanLevel.STRONG,
    "Max": FanLevel.DEEP,
    "Ultra": FanLevel.MAX,
}
FAN_SPEED_LIST = list(FAN_SPEED_MAP)
FAN_SPEED_REVERSE = {v: k for k, v in FAN_SPEED_MAP.items()}
FAN_SPEED_ALIASES: dict[str, FanLevel] = {**FAN_SPEED_MAP, "Strong": FanLevel.STRONG}

# Clean mode names are also used by the Lovelace card and send_command.
CLEAN_MODE_MAP: dict[str, CleanMode] = {
    "Freo Mind": CleanMode.FREO_MIND,
    "Vacuum & Mop": CleanMode.VACUUM_AND_MOP,
    "Vacuum then Mop": CleanMode.VACUUM_THEN_MOP,
    "Vacuum Only": CleanMode.VACUUM_ONLY,
    "Mop Only": CleanMode.MOP_ONLY,
}
CLEAN_MODE_LIST = list(CLEAN_MODE_MAP)
CLEAN_MODE_REVERSE = {v: k for k, v in CLEAN_MODE_MAP.items()}

COVERAGE_OPTIONS = ["standard", "intensive"]

MOP_HUMIDITY_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}

CONSUMABLE_NAMES = {
    "CONSUMABLE_MAINTAIN_ITEM_DUST_BOX": "Clean the dust bin",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_FILTER": "Clean the dust bin filter",
    "CONSUMABLE_MAINTAIN_ITEM_DIRTY_WATER": "Empty the dirty water tank",
    "CONSUMABLE_MAINTAIN_ITEM_WASH_RIBS": "Clean the base station tray",
    "CONSUMABLE_MAINTAIN_ITEM_UNIVERSAL_WHEEL": "Clean the caster wheel",
    "CONSUMABLE_MAINTAIN_ITEM_CLIFF_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_SIDE_DISTANCE_SENSOR": "Clean the sensors",
    "CONSUMABLE_MAINTAIN_ITEM_ANTI_WINDING_BRUSH": "Clean the roller brush",
    "CONSUMABLE_MAINTAIN_ITEM_WATER_TANK_SPONGE": "Clean the water tank filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_SMART_MODULE_SPONGE": "Clean the filter sponge",
    "CONSUMABLE_MAINTAIN_ITEM_DUST_CONTAINER": "Empty the dust bin / dust bag",
    "CONSUMABLE_REPLACE_ITEM_DUST_FILTER": "Replace the dust bin filter",
    "CONSUMABLE_REPLACE_ITEM_MOP": "Replace the mops",
    "CONSUMABLE_REPLACE_ITEM_SIDE_BRUSH": "Replace the side brush",
    "CONSUMABLE_REPLACE_ITEM_ROLLER_BRUSH": "Replace the roller brush",
    "CONSUMABLE_REPLACE_ITEM_DETERGENT": "Refill the detergent",
    "CONSUMABLE_REPLACE_ITEM_DUST_BAG": "Replace the dust bag",
}

CONF_NOTIFY_TARGETS = "notify_targets"
CONF_PUSH_ALERTS = "push_alerts"
