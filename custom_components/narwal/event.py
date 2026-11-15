"""Event entity that records the robot's alerts."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NarwalConfigEntry
from .alerts import ALERT_TYPES, Alert
from .coordinator import NarwalCoordinator
from .entity import NarwalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NarwalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([NarwalAlertEvent(entry.runtime_data)])


class NarwalAlertEvent(NarwalEntity, EventEntity):
    """Fires once per alert, with its message and details as attributes."""

    _attr_translation_key = "alerts"
    _attr_icon = "mdi:bell-ring-outline"
    _attr_event_types = ALERT_TYPES

    def __init__(self, coordinator: NarwalCoordinator) -> None:
        super().__init__(coordinator, "alerts")

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.alerts.add_listener(self._handle_alert))

    @callback
    def _handle_alert(self, alert: Alert) -> None:
        self._trigger_event(alert.type, {"message": alert.message, **alert.data})
        self.async_write_ha_state()
