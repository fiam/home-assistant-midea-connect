"""Shared Wi-Fi credentials for provisioning, separate from AC token exports."""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .bluetooth_provision import WifiCredentials
from .bluetooth_transport import BluetoothSetupError, validate_wifi
from .const import DOMAIN

STORAGE_KEY = f"{DOMAIN}.wifi_credentials"


class WifiCredentialStore:
    """Keep passwords in HA's private storage, never in form defaults."""

    def __init__(self, hass: HomeAssistant):
        self._store = Store(hass, 1, STORAGE_KEY,
                            private=True, atomic_writes=True)
        self._lock = asyncio.Lock()

    async def _async_load(self):
        data = await self._store.async_load() or {}
        profiles = {}
        for ssid, password in data.get("networks", {}).items():
            if not isinstance(ssid, str) or not isinstance(password, str):
                continue
            try:
                validate_wifi(ssid, password)
            except BluetoothSetupError:
                continue
            profiles[ssid] = password
        return profiles, data.get("last_used")

    async def async_networks(self):
        """Return only names, with the last successfully used network first."""
        async with self._lock:
            profiles, last_used = await self._async_load()
        return sorted(profiles, key=lambda ssid: (ssid != last_used, ssid.casefold()))

    async def async_get(self, ssid):
        """Return a disposable copy; clearing it cannot erase the stored value."""
        async with self._lock:
            profiles, _ = await self._async_load()
        if ssid not in profiles:
            return None
        return WifiCredentials(ssid, profiles[ssid])

    async def async_save(self, credentials):
        """Replace a network password only after successful provisioning."""
        validate_wifi(credentials.ssid, credentials.password)
        async with self._lock:
            profiles, _ = await self._async_load()
            profiles[credentials.ssid] = credentials.password
            await self._store.async_save({
                "networks": profiles, "last_used": credentials.ssid,
            })


@callback
def async_get_wifi_store(hass: HomeAssistant) -> WifiCredentialStore:
    """Share one lock across concurrent AC setup flows."""
    if STORAGE_KEY not in hass.data:
        hass.data[STORAGE_KEY] = WifiCredentialStore(hass)
    return hass.data[STORAGE_KEY]
