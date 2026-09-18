"""Wi-Fi secrets survive HA restart without leaking into forms or exports."""
import asyncio

from custom_components.midea_connect.bluetooth_provision import WifiCredentials
from custom_components.midea_connect.wifi_credentials import (
    WifiCredentialStore, async_get_wifi_store)


async def test_shared_store_merges_concurrent_devices_and_survives_reload(hass):
    store = async_get_wifi_store(hass)
    assert store is async_get_wifi_store(hass)
    await asyncio.gather(
        store.async_save(WifiCredentials("Home", "Private123")),
        store.async_save(WifiCredentials("Guest", "Guest1234")),
    )
    restored = WifiCredentialStore(hass)
    assert set(await restored.async_networks()) == {"Home", "Guest"}
    credentials = await restored.async_get("Home")
    assert credentials.password == "Private123"
    credentials.clear()
    assert (await restored.async_get("Home")).password == "Private123"
    assert await restored.async_get("Unknown") is None
