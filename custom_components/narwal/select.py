"""Select entities for Narwal robot vacuum."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .const import (
    CLEAN_MODE_LIST,
    CLEAN_MODE_MAP,
    CLEAN_MODE_REVERSE,
    COVERAGE_OPTIONS,
    FAN_SPEED_MAP,
    MOP_HUMIDITY_MAP,
)
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity
from .narwal_client import FanLevel


@dataclass(frozen=True, kw_only=True)
class NarwalConfigSelectDescription(SelectEntityDescription):
    config_key: str
    # HA option -> enum label sent to / read from the robot.
    values: dict[str, str]


CONFIG_SELECTS: tuple[NarwalConfigSelectDescription, ...] = (
    NarwalConfigSelectDescription(
        key="mop_dry_strength",
        translation_key="mop_dry_strength",
        icon="mdi:fan",
        config_key="dryMopStrength",
        values={
            "quiet": "DRY_MOP_STRENGTH_QUIET",
            "strong": "DRY_MOP_STRENGTH_STRONG",
            "smart": "DRY_MOP_STRENGTH_SMART",
        },
    ),
    NarwalConfigSelectDescription(
        key="mop_wash_frequency",
        translation_key="mop_wash_frequency",
        icon="mdi:washing-machine",
        config_key="cleanMopFreq",
        values={
            "efficient": "CLEAN_MOP_FREQ_EFFICIENT",
            "normal": "CLEAN_MOP_FREQ_NORMAL",
            "deep": "CLEAN_MOP_FREQ_DEEP",
        },
    ),
    NarwalConfigSelectDescription(
        key="carpet_strategy",
        translation_key="carpet_strategy",
        icon="mdi:rug",
        config_key="carpetCleanOption",
        values={
            "cross": "CARPET_CLEAN_OPTION_ACROSS",
            "boost": "CARPET_CLEAN_OPTION_PRESSURE",
            "ignore": "CARPET_CLEAN_OPTION_IGNORE",
            "avoid": "CARPET_CLEAN_OPTION_AVOID",
        },
    ),
    NarwalConfigSelectDescription(
        key="new_carpet_strategy",
        translation_key="new_carpet_strategy",
        icon="mdi:rug",
        config_key="addCarpetCleanOption",
        values={
            "cross": "CARPET_CLEAN_OPTION_ACROSS",
            "boost": "CARPET_CLEAN_OPTION_PRESSURE",
            "ignore": "CARPET_CLEAN_OPTION_IGNORE",
            "avoid": "CARPET_CLEAN_OPTION_AVOID",
        },
    ),
    NarwalConfigSelectDescription(
        key="obstacle_avoidance",
        translation_key="obstacle_avoidance",
        icon="mdi:sign-caution",
        config_key="avoidMode",
        values={
            "smart": "AVOID_MODE_SMART",
            "safer": "AVOID_MODE_SAFER",
            "off": "AVOID_MODE_OFF",
        },
    ),
    NarwalConfigSelectDescription(
        key="freo_strategy",
        translation_key="freo_strategy",
        icon="mdi:brain",
        config_key="smartCleanSetting",
        values={
            "quick": "SMART_CLEAN_QUICK",
            "standard": "SMART_CLEAN_NORMAL",
            "deep": "SMART_CLEAN_DEEP",
        },
    ),
)


_FAN_LABEL = {
    FanLevel.QUIET: "MUTE",
    FanLevel.NORMAL: "NORMAL",
    FanLevel.STRONG: "STRONG",
    FanLevel.DEEP: "DEEP",
    FanLevel.MAX: "SUPER",
}


@dataclass(frozen=True, kw_only=True)
class NarwalFreoSelectDescription(SelectEntityDescription):
    # smartCleanParam strategy message and the field it customises; "smart" leaves it to the robot.
    strategy: str
    field: str
    values: dict[str, Any]


FREO_SELECTS: tuple[NarwalFreoSelectDescription, ...] = (
    NarwalFreoSelectDescription(
        key="freo_suction",
        translation_key="freo_suction",
        icon="mdi:fan",
        strategy="fanLevelStrategy",
        field="fanLevel",
        values={"smart": None, **{name.lower(): f"FAN_LEVEL_{_FAN_LABEL[level]}" for name, level in FAN_SPEED_MAP.items()}},
    ),
    NarwalFreoSelectDescription(
        key="freo_mop_wetness",
        translation_key="freo_mop_wetness",
        icon="mdi:water-percent",
        strategy="mopHumidityStrategy",
        field="mopHumidity",
        values={"smart": None, **{name: f"MOP_HUMIDITY_{name.upper()}" for name in MOP_HUMIDITY_MAP}},
    ),
    NarwalFreoSelectDescription(
        key="freo_coverage",
        translation_key="freo_coverage",
        icon="mdi:texture-box",
        strategy="overlapLevelStrategy",
        field="overlapLevel",
        values={"smart": None, "standard": "OVERLAP_LEVEL_NORMAL", "intensive": "OVERLAP_LEVEL_DENSE"},
    ),
)

# Corner cleaning: option -> (CleanCornerFreq, interval). The robot reports "every day" with no interval.
CORNER_OPTIONS: dict[str, tuple[str, int]] = {
    "off": ("CLEAN_CORNER_FREQ_OFF", 0),
    "every_time": ("CLEAN_CORNER_FREQ_EVERY_N_TIMES", 1),
    "every_day": ("CLEAN_CORNER_FREQ_EVERY_N_DAYS", 1),
    "every_7_days": ("CLEAN_CORNER_FREQ_EVERY_N_DAYS", 7),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = [
        NarwalCleanModeSelect(coordinator),
        NarwalMopHumiditySelect(coordinator),
        NarwalCoverageSelect(coordinator),
        NarwalCornerSelect(coordinator),
    ]
    entities += [NarwalConfigSelect(coordinator, d) for d in CONFIG_SELECTS]
    entities += [NarwalFreoSelect(coordinator, d) for d in FREO_SELECTS]
    async_add_entities(entities)


class NarwalCleanModeSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Clean mode used by vacuum.start and room cleaning (kept in HA)."""

    _attr_translation_key = "clean_mode"
    _attr_icon = "mdi:spray-bottle"
    _attr_options = CLEAN_MODE_LIST

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "clean_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_MODE_MAP:
            self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[last.state]

    @property
    def available(self) -> bool:
        return True  # local preference; usable even while the robot sleeps

    @property
    def current_option(self) -> str:
        return CLEAN_MODE_REVERSE[self.coordinator.selected_clean_mode]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.selected_clean_mode = CLEAN_MODE_MAP[option]
        self.async_write_ha_state()


class NarwalMopHumiditySelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Mop wetness: the robot's live value while cleaning, else what the next clean uses."""

    _attr_translation_key = "mop_humidity"
    _attr_icon = "mdi:water-percent"
    _attr_options = list(MOP_HUMIDITY_MAP)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "mop_humidity")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        client = self.coordinator.client
        if last and last.state in MOP_HUMIDITY_MAP and client.state.preferred_mop_humidity is None:
            client.state.preferred_mop_humidity = MOP_HUMIDITY_MAP[last.state]

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str:
        level = self.state_data.mop_humidity or MOP_HUMIDITY_MAP["normal"]
        return level.name.lower()

    async def async_select_option(self, option: str) -> None:
        await self._run(self.coordinator.client.set_mop_humidity(MOP_HUMIDITY_MAP[option]))
        self.async_write_ha_state()


class NarwalConfigSelect(NarwalEntity, SelectEntity):
    """An enum-valued robot setting."""

    entity_description: NarwalConfigSelectDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalConfigSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.values)

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        label = self.state_data.config.get(self.entity_description.config_key)
        return next((k for k, v in self.entity_description.values.items() if v == label), None)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        await self._run(self.coordinator.client.set_config(**{d.config_key: d.values[option]}))
        self.async_write_ha_state()


class NarwalCoverageSelect(NarwalEntity, SelectEntity, RestoreEntity):
    """Coverage precision for the next clean (the app's Standard / Intensive, kept in HA)."""

    _attr_translation_key = "coverage"
    _attr_icon = "mdi:texture-box"
    _attr_options = COVERAGE_OPTIONS

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "coverage")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in COVERAGE_OPTIONS:
            self.coordinator.intensive_coverage = last.state == "intensive"

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str:
        return "intensive" if self.coordinator.intensive_coverage else "standard"

    async def async_select_option(self, option: str) -> None:
        self.coordinator.intensive_coverage = option == "intensive"
        self.async_write_ha_state()


class NarwalCornerSelect(NarwalEntity, SelectEntity):
    """How often the robot does its intensive corner cleaning."""

    _attr_translation_key = "corner_cleaning"
    _attr_icon = "mdi:vector-square"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(CORNER_OPTIONS)

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "corner_cleaning")

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        corner = self.state_data.config.get("cleanCorner") or {}
        freq = corner.get("cleanCornerFreq", "CLEAN_CORNER_FREQ_OFF")
        interval = max(int(corner.get("interval") or 0), 1) if freq != "CLEAN_CORNER_FREQ_OFF" else 0
        return next((k for k, v in CORNER_OPTIONS.items() if v == (freq, interval)), None)

    async def async_select_option(self, option: str) -> None:
        freq, interval = CORNER_OPTIONS[option]
        await self._run(self.coordinator.client.set_config(
            cleanCornerSetting={"cleanCornerFreq": freq, "interval": interval or None},
        ))
        self.async_write_ha_state()


class NarwalFreoSelect(NarwalEntity, SelectEntity):
    """One of Freo Mind's parameters: left to the robot, or fixed."""

    entity_description: NarwalFreoSelectDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalFreoSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.values)

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def current_option(self) -> str | None:
        d = self.entity_description
        param = self.state_data.config.get("smartCleanParam")
        strategy = (param if isinstance(param, dict) else {}).get(d.strategy) or {}
        if not strategy.get("isCustomizeMode"):
            return "smart"
        return next((k for k, v in d.values.items() if v == strategy.get(d.field)), None)

    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        value = d.values[option]
        strategy = {"isCustomizeMode": True, d.field: value} if value else {}
        await self._run(self.coordinator.client.set_smart_clean_param(**{d.strategy: strategy}))
        self.async_write_ha_state()
