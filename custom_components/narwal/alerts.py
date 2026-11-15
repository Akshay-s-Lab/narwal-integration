"""Robot alerts like the Narwal app's, sent as events and optional phone pushes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import CONF_NOTIFY_TARGETS, CONF_PUSH_ALERTS, CONSUMABLE_NAMES
from .narwal_client import NarwalState
from .narwal_client.error_codes import error_message
from .narwal_client.models import ACTIVITY_DOCKED

_LOGGER = logging.getLogger(__name__)

ALERT_CLEAN_WATER = "clean_water_tank"
ALERT_DIRTY_WATER = "dirty_water_tank"
ALERT_DUST_BIN = "dust_bin"
ALERT_DETERGENT = "detergent"
ALERT_STAIN_REMOVER = "stain_remover_low"
ALERT_ERROR = "error"
ALERT_STRANDED = "stranded"
ALERT_OFFLINE = "offline"
ALERT_MAINTENANCE = "maintenance_due"
ALERT_CLEAN_STARTED = "clean_started"
ALERT_CLEAN_FINISHED = "clean_finished"
ALERT_CLEAN_ENDED_EARLY = "clean_ended_early"
ALERT_CLEAN_STOPPED = "clean_stopped"

ALERT_TYPES = [
    ALERT_CLEAN_WATER,
    ALERT_DIRTY_WATER,
    ALERT_DUST_BIN,
    ALERT_DETERGENT,
    ALERT_STAIN_REMOVER,
    ALERT_ERROR,
    ALERT_STRANDED,
    ALERT_OFFLINE,
    ALERT_MAINTENANCE,
    ALERT_CLEAN_STARTED,
    ALERT_CLEAN_FINISHED,
    ALERT_CLEAN_ENDED_EARLY,
    ALERT_CLEAN_STOPPED,
]
DEFAULT_PUSH_ALERTS = [a for a in ALERT_TYPES if a != ALERT_CLEAN_STOPPED]

STRANDED_AFTER = timedelta(minutes=20)
OFFLINE_AFTER = timedelta(minutes=15)
# The robot updates its end-of-task reason a moment after the task ends.
TASK_END_SETTLE = timedelta(seconds=15)
STAIN_REMOVER_LOW_PERCENT = 15
ERROR_REPEAT_AFTER = timedelta(hours=6)

_TANK_MESSAGES: dict[str, dict[str, str]] = {
    "cleanWaterTankState": {
        "CLEAN_WATER_TANK_STATE_EMPTY": "The clean water tank is empty. Refill it.",
        "CLEAN_WATER_TANK_STATE_NOT_INSTALLED": "The clean water tank is not in the base station.",
        "CLEAN_WATER_TANK_STATE_ABNORMAL": "The clean water tank needs checking.",
    },
    "sewageTankState": {
        "SEWAGE_TANK_STATE_FILLED_OR_NOT_INSTALLED": "The dirty water tank is full or not in place. Empty it.",
        "SEWAGE_TANK_STATE_ABNORMAL": "The dirty water tank needs checking.",
    },
    "dustBoxState": {
        "DUST_BOX_STATE_SUGGEST_CLEAN_UP": "The robot's dust bin should be emptied.",
        "DUST_BOX_STATE_NOT_INSTALLED": "The robot's dust bin is not in place.",
        "DUST_BOX_STATE_ABNORMAL": "The robot's dust bin needs checking.",
    },
    "detergentState": {
        "DETERGENT_STATE_RUNNING_OUT": "The detergent is running low.",
        "DETERGENT_STATE_INSTALLED_AND_EMPTY": "The detergent is empty. Refill it.",
    },
}
_TANK_ALERTS = {
    "cleanWaterTankState": ALERT_CLEAN_WATER,
    "sewageTankState": ALERT_DIRTY_WATER,
    "dustBoxState": ALERT_DUST_BIN,
    "detergentState": ALERT_DETERGENT,
}

_FINISHED_RESULTS = {"TASK_RESULT_NORMAL_END", "TASK_RESULT_NORMAL_END_WITH_REPLENISH_MAP_FAIL"}
_STOPPED_RESULTS = {
    "TASK_RESULT_USER_FORCE_END",
    "TASK_RESULT_USER_FORCE_END_ON_STATION",
    "TASK_RESULT_USER_FORCE_END_ON_APP",
    "TASK_RESULT_RECALL_END",
}
_EARLY_END_REASONS = {
    "TASK_RESULT_SHUTDOWN_FORCE_END": "the robot shut down",
    "TASK_RESULT_LOW_BATTERY_FORCE_END": "the battery ran low",
    "TASK_RESULT_OVERFLOW_FORCE_END": "a tank overflowed",
    "TASK_RESULT_PAUSE_TOO_LONG_FORCE_END": "it was paused for too long",
    "TASK_RESULT_SYSTEM_ERROR_FORCE_END": "of a system error",
    "TASK_RESULT_MAP_UNMATCHED_FORCE_END": "it could not match its map",
    "TASK_RESULT_RELOCATION_FAIL_ON_MAP_WITH_STATION_FORCE_END": "it could not find its position",
    "TASK_RESULT_RELOCATION_FAIL_ON_MAP_WITHOUT_STATION_FORCE_END": "it could not find its position",
    "TASK_RESULT_PRETASK_ERROR_FORCE_END": "of a problem before it started",
    "TASK_RESULT_SCHEDULE_TASK_FORCE_END": "a scheduled task took over",
    "TASK_RESULT_UNEXECUTED": "it could not run",
}


@dataclass(frozen=True)
class Alert:
    """One alert: its type, a readable message and details for automations."""

    type: str
    title: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Snapshot:
    base: dict[str, Any]
    cleaning: bool
    error_codes: set[int]
    due: set[str]
    area: float | None
    seconds: int


class AlertTracker:
    """Turns robot state changes into alerts and delivers them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, state: Callable[[], NarwalState]) -> None:
        self.hass = hass
        self.entry = entry
        self._state = state
        self._listeners: list[Callable[[Alert], None]] = []
        self._previous: _Snapshot | None = None
        self._stranded_unsub: CALLBACK_TYPE | None = None
        self._offline_unsub: CALLBACK_TYPE | None = None
        self._task_end_unsub: CALLBACK_TYPE | None = None
        self._stranded_sent = False
        self._offline_sent = False
        self._errors_alerted: dict[int, float] = {}

    @callback
    def add_listener(self, listener: Callable[[Alert], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def stop(self) -> None:
        for unsub in (self._stranded_unsub, self._offline_unsub, self._task_end_unsub):
            if unsub:
                unsub()

    @callback
    def update(self) -> None:
        state = self._state()
        self._watch_offline(state)
        if not state.device_reachable or not state.base:
            return
        now = _Snapshot(
            base=dict(state.base),
            cleaning=state.is_cleaning,
            error_codes=set(state.error_codes),
            due=set(state.consumables_due.get("maintain", []) + state.consumables_due.get("replace", [])),
            area=state.cleaned_area,
            seconds=state.task_seconds,
        )
        before, self._previous = self._previous, now
        self._watch_stranded(state)
        if before is None:
            return
        self._check_supplies(before, now)
        self._check_errors(before, now)
        self._check_maintenance(before, now)
        self._check_task(before, now)

    def _check_supplies(self, before: _Snapshot, now: _Snapshot) -> None:
        for field_name, messages in _TANK_MESSAGES.items():
            value = now.base.get(field_name)
            if value in messages and before.base.get(field_name) != value:
                self._emit(_TANK_ALERTS[field_name], messages[value], state=value)
        percent = now.base.get("heavyDetergentRemainPercent")
        previous = before.base.get("heavyDetergentRemainPercent")
        if (
            isinstance(percent, int)
            and isinstance(previous, int)
            and percent < STAIN_REMOVER_LOW_PERCENT <= previous
        ):
            self._emit(ALERT_STAIN_REMOVER, f"The stain remover is down to {percent}%.", percent=percent)

    def _check_errors(self, before: _Snapshot, now: _Snapshot) -> None:
        clock = time.monotonic()
        quiet_until = clock - ERROR_REPEAT_AFTER.total_seconds()
        new = sorted(
            code for code in now.error_codes - before.error_codes
            if self._errors_alerted.get(code, float("-inf")) < quiet_until
        )
        if new:
            self._errors_alerted.update(dict.fromkeys(new, clock))
            messages = [error_message(code) for code in new]
            self._emit(
                ALERT_ERROR,
                " ".join(messages),
                codes=[f"0x{code:08X}" for code in new],
                messages=messages,
            )

    def _check_maintenance(self, before: _Snapshot, now: _Snapshot) -> None:
        new = sorted(now.due - before.due)
        if new:
            items = sorted({CONSUMABLE_NAMES.get(item, item) for item in new})
            self._emit(ALERT_MAINTENANCE, "Maintenance due: " + "; ".join(items) + ".", items=items)

    def _check_task(self, before: _Snapshot, now: _Snapshot) -> None:
        if now.cleaning and not before.cleaning:
            if self._task_end_unsub:
                self._task_end_unsub()
                self._task_end_unsub = None
            self._emit(ALERT_CLEAN_STARTED, "The robot started cleaning.")
        elif before.cleaning and not now.cleaning:
            area, seconds = before.area, before.seconds
            if self._task_end_unsub:
                self._task_end_unsub()
            self._task_end_unsub = async_call_later(
                self.hass, TASK_END_SETTLE, callback(lambda _now: self._report_task_end(area, seconds))
            )

    @callback
    def _report_task_end(self, area: float | None, seconds: int) -> None:
        self._task_end_unsub = None
        reason = self._state().base.get("terminateReason")
        minutes = round(seconds / 60) if seconds else None
        summary = _summary(area, minutes)
        details = {"area": area, "minutes": minutes, "reason": reason}
        if reason in _STOPPED_RESULTS:
            self._emit(ALERT_CLEAN_STOPPED, f"Cleaning was stopped{summary}.", **details)
        elif reason in _EARLY_END_REASONS:
            self._emit(
                ALERT_CLEAN_ENDED_EARLY,
                f"Cleaning ended early because {_EARLY_END_REASONS[reason]}{summary}.",
                **details,
            )
        else:
            self._emit(ALERT_CLEAN_FINISHED, f"Cleaning finished{summary}.", **details)

    def _watch_stranded(self, state: NarwalState) -> None:
        stranded = not state.is_cleaning and not state.on_station and state.activity != ACTIVITY_DOCKED
        if not stranded:
            self._stranded_sent = False
            if self._stranded_unsub:
                self._stranded_unsub()
                self._stranded_unsub = None
            return
        if self._stranded_unsub or self._stranded_sent:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._stranded_unsub = None
            self._stranded_sent = True
            battery = self._state().battery_level
            level = f" Battery is at {battery:.0f}%." if battery is not None else ""
            self._emit(ALERT_STRANDED, f"The robot has been off its dock and idle for a while.{level}", battery=battery)

        self._stranded_unsub = async_call_later(self.hass, STRANDED_AFTER, _fire)

    def _watch_offline(self, state: NarwalState) -> None:
        if state.device_reachable:
            if self._offline_unsub:
                self._offline_unsub()
                self._offline_unsub = None
            self._offline_sent = False
            return
        if self._offline_unsub or self._offline_sent:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._offline_unsub = None
            self._offline_sent = True
            self._emit(ALERT_OFFLINE, "Home Assistant has lost contact with the robot.")

        self._offline_unsub = async_call_later(self.hass, OFFLINE_AFTER, _fire)

    @callback
    def _emit(self, alert_type: str, message: str, **data: Any) -> None:
        alert = Alert(alert_type, "Narwal", message, data)
        _LOGGER.debug("Narwal alert %s: %s", alert_type, message)
        for listener in list(self._listeners):
            listener(alert)
        self._push(alert)

    def _push(self, alert: Alert) -> None:
        if alert.type not in self.entry.options.get(CONF_PUSH_ALERTS, DEFAULT_PUSH_ALERTS):
            return
        for service in notify_targets(self.hass, self.entry):
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "notify",
                    service,
                    {
                        "title": alert.title,
                        "message": alert.message,
                        "data": {"tag": f"narwal_{alert.type}", "group": "narwal"},
                    },
                ),
                f"narwal push {alert.type} to {service}",
            )


def mobile_app_services(hass: HomeAssistant) -> list[str]:
    """Every Companion app notify service registered right now."""
    return sorted(s for s in hass.services.async_services_for_domain("notify") if s.startswith("mobile_app_"))


def notify_targets(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The chosen phones, or every Companion app phone when none are chosen."""
    available = mobile_app_services(hass)
    chosen = entry.options.get(CONF_NOTIFY_TARGETS) or []
    return [s for s in chosen if s in available] if chosen else available


def _summary(area: float | None, minutes: int | None) -> str:
    parts = []
    if area:
        parts.append(f"{area:g} m²")
    if minutes:
        parts.append(f"{minutes} min")
    return ": " + " in ".join(parts) if parts else ""
