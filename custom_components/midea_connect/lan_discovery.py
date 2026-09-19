"""Bounded local address recovery using device identity and saved credentials."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from msmart.discover import Discover
from msmart.lan import AuthenticationError, ProtocolError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_SCAN_INTERVAL = 60
_STORE_KEY = f"{DOMAIN}.address_discovery"


class AddressDiscovery:
    """Share scans and serialize verification across unavailable ACs."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._next_scan = 0.0
        self._devices = []
        self._next_verify = {}

    async def recover(self, hass: HomeAssistant, entry: ConfigEntry) -> bool:
        async with self._lock:
            now = monotonic()
            if now < self._next_verify.get(entry.entry_id, 0):
                return False
            self._next_verify[entry.entry_id] = now + _SCAN_INTERVAL
            try:
                if now >= self._next_scan:
                    self._next_scan = now + _SCAN_INTERVAL
                    self._devices = []
                    async with asyncio.timeout(10):
                        self._devices = await Discover.discover(auto_connect=False, timeout=3)
                original = dict(entry.data)
                matches = [device for device in self._devices
                           if str(device.id) == str(original['id'])
                           and device.type == original['device_type']
                           and (not original.get('sn') or device.sn == original['sn'])]
                # Conflicting advertisements must never change the saved host.
                if len(matches) != 1:
                    return False
                device = matches[0]
                if device.ip == original['host'] and device.port == original['port']:
                    return False
                try:
                    async with asyncio.timeout(15):
                        if original.get('token') and original.get('k1'):
                            await device.authenticate(original['token'], original['k1'])
                        elif device.version == 3:
                            return False
                        await device.refresh()
                        if not device.online:
                            return False
                finally:
                    device._lan._disconnect()
                # Do not overwrite a concurrent reconfiguration or a deleted entry.
                if (hass.config_entries.async_get_entry(entry.entry_id) is not entry
                        or dict(entry.data) != original):
                    return False
                hass.config_entries.async_update_entry(entry, data={
                    **original, 'host': device.ip, 'port': device.port,
                })
                _LOGGER.info("Recovered LAN address for %s: %s → %s",
                             entry.title, original['host'], device.ip)
                return True
            except (OSError, TimeoutError, AuthenticationError, ProtocolError):
                # An unavailable device or invalid token cannot change the entry.
                return False


async def async_recover_address(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    discovery = hass.data.setdefault(_STORE_KEY, AddressDiscovery())
    return await discovery.recover(hass, entry)
