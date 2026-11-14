"""DataUpdateCoordinator for Narwal integration."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BROKER_HOST,
    CONF_BROKER_PORT,
    CONF_BROKER_TLS,
    CONF_CONNECTION,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_MQTT_CLIENT_ID,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_PASSWORD,
    CONF_PRODUCT_KEY,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_ROOMS_CACHE,
    CONF_USER_UUID,
    CONNECTION_LOCAL,
    DEFAULT_LOCAL_PORT,
    DOMAIN,
    LOCAL_USER_UUID,
)
from .narwal_client import (
    CleanMode,
    NarwalClient,
    NarwalCloud,
    NarwalCloudError,
    NarwalCommandError,
    NarwalConnectionError,
    NarwalState,
)
from .narwal_client.const import CONSUMABLE_LIFETIME_HOURS
from .narwal_client.models import (
    ACTIVITY_CLEANING,
    ACTIVITY_DOCKED,
    ACTIVITY_IDLE,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
    CleanPlan,
    RoomInfo,
)

_LOGGER = logging.getLogger(__name__)

_TASK_ACTIVITIES = (ACTIVITY_CLEANING, ACTIVITY_RETURNING, ACTIVITY_PAUSED)
POLL_INTERVAL_ACTIVE = timedelta(seconds=30)
POLL_INTERVAL_IDLE = timedelta(minutes=5)
MAX_CONSECUTIVE_FAILURES = 3
# Re-register with the robot before its 60 s publish window lapses.
KEEPALIVE_INTERVAL = timedelta(seconds=50)
# Settings / consumables / map change rarely; refresh on a slower cadence.
SLOW_REFRESH_SECONDS = 15 * 60
MAP_REFRESH_SECONDS = 30 * 60


class NarwalCoordinator(DataUpdateCoordinator[NarwalState]):
    """Manages communication with the Narwal vacuum via MQTT."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=POLL_INTERVAL_ACTIVE)
        self.config_entry = entry
        self._cloud: NarwalCloud | None = None
        self.selected_clean_mode: CleanMode = CleanMode.VACUUM_AND_MOP
        self.clean_passes: int = 1
        self.mop_passes: int = 1
        self.intensive_coverage = False
        self.use_stain_remover = False
        self._consecutive_failures = 0
        self._keepalive_unsub = None
        self._last_slow_refresh = 0.0
        self._last_activity: str | None = None
        self.map_version = 0  # bumped whenever the map or robot pose changes
        self._usage_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.consumables")

        if entry.data.get(CONF_CONNECTION) == CONNECTION_LOCAL:
            self._setup_from_local_config(entry)
        elif CONF_ACCESS_TOKEN in entry.data:
            self._setup_from_cloud_config(entry)
        else:
            self._setup_from_legacy_config(entry)

    def clean_plan(self, **overrides: Any) -> CleanPlan:
        """The next clean as chosen in HA, with per-call overrides (None keeps the choice)."""
        plan = CleanPlan(
            mode=self.selected_clean_mode,
            passes=self.clean_passes,
            mop_passes=self.mop_passes,
            intensive=self.intensive_coverage,
            stain_remover=self.use_stain_remover,
        )
        return replace(plan, **{k: v for k, v in overrides.items() if v is not None})

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_from_cloud_config(self, entry: ConfigEntry) -> None:
        region = entry.data.get(CONF_REGION, "us")
        self._cloud = NarwalCloud(region=region)
        self._cloud.session.access_token = entry.data[CONF_ACCESS_TOKEN]
        self._cloud.session.refresh_token = entry.data[CONF_REFRESH_TOKEN]
        self._cloud.session.user_uuid = entry.data[CONF_USER_UUID]
        self._cloud.session.region = region
        self._cloud._update_token_expiry()
        self.client = NarwalClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data[CONF_USER_UUID],
            mqtt_username=entry.data[CONF_USER_UUID],
            mqtt_password=entry.data[CONF_ACCESS_TOKEN],
            broker=self._cloud.mqtt_broker,
        )

    def _setup_from_local_config(self, entry: ConfigEntry) -> None:
        self.client = NarwalClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data.get(CONF_USER_UUID) or LOCAL_USER_UUID,
            mqtt_username=entry.data.get(CONF_MQTT_USERNAME) or None,
            mqtt_password=entry.data.get(CONF_MQTT_PASSWORD) or None,
            broker=entry.data[CONF_BROKER_HOST],
            port=entry.data.get(CONF_BROKER_PORT, DEFAULT_LOCAL_PORT),
            use_tls=entry.data.get(CONF_BROKER_TLS, False),
        )

    def _setup_from_legacy_config(self, entry: ConfigEntry) -> None:
        self.client = NarwalClient(
            product_key=entry.data[CONF_PRODUCT_KEY],
            device_name=entry.data[CONF_DEVICE_NAME],
            user_uuid=entry.data[CONF_USER_UUID],
            mqtt_username=entry.data[CONF_MQTT_USERNAME],
            mqtt_password=entry.data[CONF_MQTT_PASSWORD],
            mqtt_client_id=entry.data.get(CONF_MQTT_CLIENT_ID) or None,
        )

    async def async_setup(self) -> None:
        """Refresh the token, connect, and load what the robot will tell us."""
        cached = self.config_entry.data.get(CONF_ROOMS_CACHE, [])
        if cached:
            self.client.state.rooms = [RoomInfo(**r) for r in cached]
        await self._load_consumable_usage()

        if self._cloud:
            await self._reauth()

        self.client.on_state_update = self._on_state_update
        await self.client.connect()

        try:
            await self.client.request_status_update()
        except NarwalCommandError:
            _LOGGER.warning("Initial status request timed out — vacuum may be asleep")

        await self.client.fetch_device_info()
        await self._slow_refresh()
        await self._refresh_map()

        self._consecutive_failures = 0
        self.async_set_updated_data(self.client.state)
        self._start_keepalive()

    async def _slow_refresh(self) -> None:
        """Settings and consumables: best effort, never fatal."""
        self._last_slow_refresh = time.monotonic()
        for fetch in (self.client.fetch_config, self.client.fetch_consumables):
            try:
                await fetch()
            except (NarwalCommandError, NarwalConnectionError) as err:
                _LOGGER.debug("Slow refresh %s failed: %s", fetch.__name__, err)

    async def _refresh_map(self) -> None:
        try:
            await self.client.fetch_map()
        except (NarwalCommandError, NarwalConnectionError) as err:
            _LOGGER.debug("Map refresh failed: %s", err)
            return
        self.map_version += 1
        self._persist_rooms_cache()

    @callback
    def async_map_edited(self) -> None:
        """The robot returned an edited map: redraw it and keep the room cache current."""
        self.map_version += 1
        self._persist_rooms_cache()
        self.async_set_updated_data(self.client.state)

    def _persist_rooms_cache(self) -> None:
        rooms = self.client.state.rooms
        if not rooms:
            return
        cache = [
            {
                "room_id": r.room_id,
                "room_sub_type": r.room_sub_type,
                "name": r.name,
                "category": r.category,
                "instance_index": r.instance_index,
            }
            for r in rooms
        ]
        if self.config_entry.data.get(CONF_ROOMS_CACHE) == cache:
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_ROOMS_CACHE: cache}
        )

    def _start_keepalive(self) -> None:
        if self._keepalive_unsub is None:
            self._keepalive_unsub = async_track_time_interval(
                self.hass, self._send_keepalive, KEEPALIVE_INTERVAL
            )

    async def _send_keepalive(self, _now=None) -> None:
        if self.client.connected:
            await self.client.notify_active()

    # ------------------------------------------------------------------
    # Auth / reconnect
    # ------------------------------------------------------------------

    async def _reauth(self) -> None:
        """Refresh the token, falling back to a full login."""
        if not self._cloud:
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.refresh_token)
            self._apply_new_token(session.access_token, session.refresh_token)
            return
        except NarwalCloudError as err:
            _LOGGER.info("Token refresh failed (%s), attempting full re-login", err)

        email = self.config_entry.data.get(CONF_EMAIL)
        password = self.config_entry.data.get(CONF_PASSWORD)
        if not email or not password:
            _LOGGER.error("Cannot re-login: no stored credentials")
            return
        try:
            session = await self.hass.async_add_executor_job(self._cloud.login, email, password)
            self._apply_new_token(session.access_token, session.refresh_token)
        except NarwalCloudError as err:
            _LOGGER.error("Re-login failed: %s", err)

    def _apply_new_token(self, access_token: str, refresh_token: str) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
            },
        )
        self.client._mqtt_password = access_token

    async def _reconnect_with_fresh_token(self) -> None:
        _LOGGER.warning("Reconnecting to the MQTT broker (failures=%d)", self._consecutive_failures)
        try:
            await self._reauth()
            if self.client.connected:
                await self.client.disconnect()
            await self.client.connect()
            self._consecutive_failures = 0
        except Exception:  # noqa: BLE001 - keep the coordinator alive
            _LOGGER.error("Reconnect failed", exc_info=True)
            self.client.state.device_reachable = False
            self.async_set_updated_data(self.client.state)

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def _on_state_update(self, state: NarwalState) -> None:
        """Push from MQTT (runs on the event loop)."""
        self._consecutive_failures = 0
        activity = state.activity
        if self._last_activity in _TASK_ACTIVITIES and activity in (ACTIVITY_DOCKED, ACTIVITY_IDLE):
            # A task just finished: the map may have changed, and
            # consumable counters move after every clean.
            self.hass.async_create_task(self._after_task_finished())
        self._last_activity = activity
        if state.robot_pose is not None:
            self.map_version += 1
        self._adjust_poll_interval()
        self.async_set_updated_data(state)

    async def _after_task_finished(self) -> None:
        await self._record_task_usage()
        await self._refresh_map()
        await self._slow_refresh()
        self.async_set_updated_data(self.client.state)

    # ------------------------------------------------------------------
    # Consumable usage (cleaning time since each part was last replaced)
    # ------------------------------------------------------------------

    async def _load_consumable_usage(self) -> None:
        stored = await self._usage_store.async_load() or {}
        seconds = stored.get("seconds") or {}
        self.client.state.consumable_seconds = {
            item: float(seconds.get(item, 0.0)) for item in CONSUMABLE_LIFETIME_HOURS
        }
        self.client.state.consumable_tracking_since = stored.get("since")
        if self.client.state.consumable_tracking_since is None:
            self.client.state.consumable_tracking_since = dt_util.utcnow().isoformat()
            await self._save_consumable_usage()

    async def _save_consumable_usage(self) -> None:
        await self._usage_store.async_save({
            "seconds": self.client.state.consumable_seconds,
            "since": self.client.state.consumable_tracking_since,
        })

    async def _record_task_usage(self) -> None:
        state = self.client.state
        elapsed = state.task_seconds
        state.task_seconds = 0
        if elapsed <= 0:
            return
        for item in CONSUMABLE_LIFETIME_HOURS:
            state.consumable_seconds[item] = state.consumable_seconds.get(item, 0.0) + elapsed
        await self._save_consumable_usage()

    async def async_reset_consumable(self, item: str) -> None:
        """Mark a part as replaced, on the robot and in the local usage counter."""
        await self.client.reset_consumable(f"CONSUMABLE_REPLACE_ITEM_{item}")
        self.client.state.consumable_seconds[item] = 0.0
        await self._save_consumable_usage()
        self.async_set_updated_data(self.client.state)

    def _adjust_poll_interval(self) -> None:
        idle = self.client.state.activity in (ACTIVITY_DOCKED, ACTIVITY_IDLE)
        new_interval = POLL_INTERVAL_IDLE if idle else POLL_INTERVAL_ACTIVE
        if self.update_interval != new_interval:
            self.update_interval = new_interval

    async def _async_update_data(self) -> NarwalState:
        """Poll as a backup for pushes; also drives reconnects."""
        token_expired = self._cloud is not None and self._cloud.session.is_token_expired
        if token_expired or self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            await self._reconnect_with_fresh_token()

        if not self.client.connected:
            self._consecutive_failures += 1
            return self.client.state

        # Availability follows the MQTT connection; a robot in deep sleep is
        # reachable-but-quiet. Failures only drive the periodic reconnect.
        try:
            await self.client.request_status_update()
            self._consecutive_failures = 0
        except NarwalCommandError:
            self._consecutive_failures += 1
            _LOGGER.debug("Status poll failed (%d)", self._consecutive_failures)
            self._adjust_poll_interval()
            return self.client.state

        if time.monotonic() - self._last_slow_refresh > SLOW_REFRESH_SECONDS:
            await self._slow_refresh()
        state = self.client.state
        map_age = time.time() - state.map.fetched_at if state.map else None
        if not state.rooms or map_age is None or map_age > MAP_REFRESH_SECONDS:
            await self._refresh_map()

        self._adjust_poll_interval()
        return self.client.state

    async def async_shutdown(self) -> None:
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        await super().async_shutdown()
        await self.client.disconnect()
