"""Local broker mode: the robot's MQTT is redirected to a broker on the LAN."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.narwal.const import DOMAIN
from custom_components.narwal.narwal_client.client import NarwalBrokerAuthError, NarwalClient
from custom_components.narwal.narwal_client.cloud import NarwalCloud

from .conftest import DEVICE, PRODUCT

VACUUM = "vacuum.narwal_ehf6crnrgt_narwal_vacuum"


@pytest.fixture
def local_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=f"{PRODUCT}_{DEVICE}",
        data={
            "connection": "local",
            "broker_host": "mqtt.lan",
            "broker_port": 1883,
            "broker_tls": False,
            "mqtt_username": "homeassistant",
            "mqtt_password": "broker-secret",
            "product_key": PRODUCT,
            "device_name": DEVICE,
            "user_uuid": "0" * 32,
        },
    )


async def test_local_entry_sets_up_without_the_cloud(hass: HomeAssistant, local_entry, mock_robot) -> None:
    local_entry.add_to_hass(hass)
    with (
        patch.object(NarwalCloud, "login", side_effect=AssertionError("cloud login in local mode")),
        patch.object(NarwalCloud, "refresh_token", side_effect=AssertionError("token refresh in local mode")),
    ):
        assert await hass.config_entries.async_setup(local_entry.entry_id)
        await hass.async_block_till_done()

        assert local_entry.state is ConfigEntryState.LOADED
        coordinator = local_entry.runtime_data
        client = coordinator.client
        assert (client.broker, client.port, client.use_tls) == ("mqtt.lan", 1883, False)
        assert (client._mqtt_username, client._mqtt_password) == ("homeassistant", "broker-secret")
        assert client.user_uuid == "0" * 32
        assert hass.states.get(VACUUM).state == VacuumActivity.DOCKED

        coordinator._consecutive_failures = 3
        await coordinator._async_update_data()
        assert client.connected


def test_broker_rejection_fails_fast() -> None:
    client = NarwalClient(
        product_key=PRODUCT, device_name=DEVICE, user_uuid="0" * 32,
        mqtt_username="homeassistant", mqtt_password="wrong", broker="mqtt.lan", port=1883, use_tls=False,
    )

    def reject(self: NarwalClient) -> None:
        self._client = None
        self._on_connect(None, None, None, "Not authorized")

    async def run() -> None:
        started = time.monotonic()
        with patch.object(NarwalClient, "_setup_mqtt_client", reject), pytest.raises(NarwalBrokerAuthError):
            await client.connect()
        assert time.monotonic() - started < 2

    import asyncio

    asyncio.run(run())


def test_tls_and_credentials_are_optional() -> None:
    client = NarwalClient(
        product_key=PRODUCT, device_name=DEVICE, user_uuid="0" * 32,
        mqtt_username=None, mqtt_password=None, broker="mqtt.lan", port=1883, use_tls=False,
    )
    with patch("custom_components.narwal.narwal_client.client.mqtt.Client") as mqtt_client:
        client._setup_mqtt_client()
    instance = mqtt_client.return_value
    instance.username_pw_set.assert_not_called()
    instance.tls_set_context.assert_not_called()
    instance.connect.assert_called_once_with("mqtt.lan", 1883, keepalive=30)
