"""Switch entities for Narwal robot settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NarwalConfigEntry
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


@dataclass(frozen=True, kw_only=True)
class NarwalSwitchDescription(SwitchEntityDescription):
    # Same field name in GetConfig (read) and SetConfig (write).
    config_key: str


SWITCHES: tuple[NarwalSwitchDescription, ...] = (
    NarwalSwitchDescription(key="child_lock", translation_key="child_lock",
                            config_key="childLockEnabled", icon="mdi:lock"),
    NarwalSwitchDescription(key="auto_empty", translation_key="auto_empty",
                            config_key="dustGatheringEnabled", icon="mdi:delete-restore"),
    NarwalSwitchDescription(key="quiet_auto_empty", translation_key="quiet_auto_empty",
                            config_key="quietDustGatheringEnabled", icon="mdi:volume-low"),
    NarwalSwitchDescription(key="auto_detergent", translation_key="auto_detergent",
                            config_key="autoAddDetergentEnabled", icon="mdi:bottle-tonic-plus"),
    NarwalSwitchDescription(key="carpet_boost", translation_key="carpet_boost",
                            config_key="cleanCarpetEnabled", icon="mdi:rug"),
    NarwalSwitchDescription(key="pet_mode", translation_key="pet_mode",
                            config_key="petMode", icon="mdi:paw"),
    NarwalSwitchDescription(key="smart_deep_clean", translation_key="smart_deep_clean",
                            config_key="smartDeepCleanEnabled", icon="mdi:auto-fix"),
    NarwalSwitchDescription(key="hot_water_wash", translation_key="hot_water_wash",
                            config_key="hotWaterWashEnabled", icon="mdi:water-thermometer"),
    NarwalSwitchDescription(key="crossing_boost", translation_key="crossing_boost",
                            config_key="boostCrossingModeEnabled", icon="mdi:stairs-up"),
    NarwalSwitchDescription(key="dirt_detection", translation_key="dirt_detection",
                            config_key="smartCleanDetectionEnabled", icon="mdi:eye-outline"),
    NarwalSwitchDescription(key="heavy_dirt_deep_clean", translation_key="heavy_dirt_deep_clean",
                            config_key="massiveDirtyDeepCleanEnable", icon="mdi:texture"),
    NarwalSwitchDescription(key="pad_moisture_protection", translation_key="pad_moisture_protection",
                            config_key="moistureProofPadProtectEnabled", icon="mdi:water-off"),
    NarwalSwitchDescription(key="dust_bag_drying", translation_key="dust_bag_drying",
                            config_key="dryRobotBagEnabled", icon="mdi:sun-thermometer"),
    NarwalSwitchDescription(key="auto_power_off", translation_key="auto_power_off",
                            config_key="automaticPoweroffWhenDischarging", icon="mdi:power"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [NarwalConfigSwitch(coordinator, d) for d in SWITCHES]
    entities.append(NarwalDndSwitch(coordinator))
    entities.append(NarwalStainRemoverSwitch(coordinator))
    async_add_entities(entities)


class NarwalConfigSwitch(NarwalEntity, SwitchEntity):
    """A boolean robot setting."""

    entity_description: NarwalSwitchDescription
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator, description: NarwalSwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # Settings are only known once config/get has answered.
        return super().available and bool(self.state_data.config)

    @property
    def is_on(self) -> bool:
        # proto3: an absent bool means False.
        return bool(self.state_data.config.get(self.entity_description.config_key, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        await self._run(self.coordinator.client.set_config(**{self.entity_description.config_key: value}))
        self.async_write_ha_state()


class NarwalDndSwitch(NarwalEntity, SwitchEntity):
    """Do-not-disturb on/off (keeps the robot's configured time window)."""

    _attr_translation_key = "do_not_disturb"
    _attr_icon = "mdi:moon-waning-crescent"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "do_not_disturb")

    @property
    def _dnd(self) -> dict[str, Any]:
        value = self.state_data.config.get("disturbMode")
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.state_data.config)

    @property
    def is_on(self) -> bool:
        return bool(self._dnd.get("enabled", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        def fmt(seconds: Any) -> str | None:
            if not isinstance(seconds, int):
                return None
            return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"

        return {"start": fmt(self._dnd.get("beginTime")), "end": fmt(self._dnd.get("endTime"))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        dnd = {
            "enabled": enabled,
            "beginTime": self._dnd.get("beginTime", 22 * 3600),
            "endTime": self._dnd.get("endTime", 8 * 3600),
        }
        await self._run(self.coordinator.client.set_config(disturbMode=dnd))
        self.async_write_ha_state()


class NarwalStainRemoverSwitch(NarwalEntity, SwitchEntity, RestoreEntity):
    """Use the heavy-duty stain remover in the next clean (kept in HA)."""

    _attr_translation_key = "stain_remover"
    _attr_icon = "mdi:spray"

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "stain_remover")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last:
            self.coordinator.use_stain_remover = last.state == "on"

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.use_stain_remover

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.use_stain_remover = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.use_stain_remover = False
        self.async_write_ha_state()
