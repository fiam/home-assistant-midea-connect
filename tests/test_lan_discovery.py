"""Recover moved ACs locally without accepting a different device or key."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from msmart.lan import AuthenticationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect.const import DOMAIN
from custom_components.midea_connect.lan_discovery import async_recover_address
from custom_components.midea_connect.coordinator import MideaDeviceUpdateCoordinator


def entry(hass, device_id=1234):
    e = MockConfigEntry(domain=DOMAIN, data={'id': device_id, 'device_type': 0xAC,
        'host': '192.0.2.10', 'port': 6444, 'sn': f'{device_id:032}',
        'token': 'ab' * 64, 'k1': 'cd' * 32, 'account_entry_id': 'saved-account'},
        options={'update_interval': 20})
    e.add_to_hass(hass)
    return e


def device(e, **kwargs):
    return SimpleNamespace(id=e.data['id'], type=0xAC, sn=e.data['sn'],
        ip=kwargs.pop('ip', '192.0.2.20'), port=6444, version=3,
        online=True, authenticate=AsyncMock(), refresh=AsyncMock(),
        _lan=SimpleNamespace(_disconnect=MagicMock()), **kwargs)


async def test_moved_device_verifies_saved_keys_and_updates_only_address(hass):
    e = entry(hass)
    d = device(e)
    original = dict(e.data)
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[d]) as discover:
        assert await async_recover_address(hass, e)
    discover.assert_awaited_once_with(auto_connect=False, timeout=3)
    d.authenticate.assert_awaited_once_with(original['token'], original['k1'])
    d.refresh.assert_awaited_once()
    d._lan._disconnect.assert_called_once()
    assert dict(e.data) == {**original, 'host': d.ip}
    assert e.options == {'update_interval': 20}


@pytest.mark.parametrize('mismatch', ['id', 'sn', 'type', 'duplicate', 'same_ip', 'bad_key', 'offline', 'missing_keys'])
async def test_unverified_or_ambiguous_device_never_changes_address(hass, mismatch):
    e = entry(hass)
    d = device(e)
    if mismatch in ('id', 'type'):
        setattr(d, mismatch, 9999)
    elif mismatch == 'sn':
        d.sn = 'other'
    elif mismatch == 'same_ip':
        d.ip = e.data['host']
    elif mismatch == 'bad_key':
        d.authenticate.side_effect = AuthenticationError('rejected')
    elif mismatch == 'offline':
        d.online = False
    elif mismatch == 'missing_keys':
        hass.config_entries.async_update_entry(e, data={**e.data, 'token': None, 'k1': None})
    original = dict(e.data)
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[d, d] if mismatch == 'duplicate' else [d]):
        assert not await async_recover_address(hass, e)
    assert dict(e.data) == original


async def test_concurrent_failures_share_scan_and_throttle_retries(hass):
    a, b = entry(hass, 1234), entry(hass, 5678)
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[]) as scan:
        assert await asyncio.gather(async_recover_address(hass, a), async_recover_address(hass, b)) == [False, False]
        assert not await async_recover_address(hass, a)
        scan.assert_awaited_once()
    with patch('custom_components.midea_connect.lan_discovery.monotonic', return_value=10**12), patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[]) as scan:
        assert not await async_recover_address(hass, a)
        scan.assert_awaited_once()


async def test_concurrent_reconfiguration_is_not_overwritten(hass):
    e = entry(hass)
    d = device(e)
    async def refresh():
        hass.config_entries.async_update_entry(e, data={**e.data, 'host': '192.0.2.30'})
    d.refresh.side_effect = refresh
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[d]):
        assert not await async_recover_address(hass, e)
    assert e.data['host'] == '192.0.2.30'


async def test_scan_failure_keeps_config_and_is_throttled(hass):
    e = entry(hass)
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', side_effect=OSError) as scan:
        assert not await async_recover_address(hass, e)
        assert not await async_recover_address(hass, e)
    scan.assert_awaited_once()
    assert e.data['host'] == '192.0.2.10'


async def test_runtime_offline_scan_triggers_existing_entry_listener(hass):
    e = entry(hass)
    original = device(e, ip=e.data['host'])
    original.online = False
    coordinator = MideaDeviceUpdateCoordinator(hass, original, address_entry=e)
    hass.data.setdefault(DOMAIN, {})[e.entry_id] = coordinator
    changed = AsyncMock()
    unsubscribe = e.add_update_listener(changed)
    try:
        with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[device(e)]):
            await coordinator._async_update_data()
        await hass.async_block_till_done()
        changed.assert_awaited_once_with(hass, e)
    finally:
        unsubscribe()
        await coordinator.async_shutdown()


async def test_first_refresh_retries_startup_after_address_change(hass):
    e = entry(hass)
    old = device(e)
    old.online = False
    coordinator = MideaDeviceUpdateCoordinator(hass, old, address_entry=e)
    with patch('custom_components.midea_connect.lan_discovery.Discover.discover', return_value=[device(e)]):
        with pytest.raises(UpdateFailed, match='address changed'):
            await coordinator._async_update_data()
    await coordinator.async_shutdown()


async def test_healthy_device_does_not_scan(hass):
    e = entry(hass)
    coordinator = MideaDeviceUpdateCoordinator(hass, device(e), address_entry=e)
    with patch('custom_components.midea_connect.coordinator.async_recover_address') as recover:
        await coordinator._async_update_data()
    recover.assert_not_called()
    await coordinator.async_shutdown()


async def test_startup_auth_failure_attempts_local_recovery(hass):
    from msmart.device import AirConditioner
    from custom_components.midea_connect import async_setup_entry
    e = entry(hass)
    old = AirConditioner(ip=e.data['host'], port=6444, device_id=1234)
    with (patch('custom_components.midea_connect.Device.construct', return_value=old),
          patch('custom_components.midea_connect.PushAirConditioner', return_value=old),
          patch.object(old, 'authenticate', side_effect=AuthenticationError('stale host')),
          patch('custom_components.midea_connect.async_recover_address', return_value=True) as recover):
        with pytest.raises(ConfigEntryNotReady, match='address changed'):
            await async_setup_entry(hass, e)
    recover.assert_awaited_once_with(hass, e)


async def test_refresh_network_exception_also_triggers_recovery(hass):
    e = entry(hass)
    d = device(e)
    d.refresh.side_effect = OSError('disconnected')
    coordinator = MideaDeviceUpdateCoordinator(hass, d, address_entry=e)
    with patch('custom_components.midea_connect.coordinator.async_recover_address', return_value=False) as recover:
        with pytest.raises(UpdateFailed, match='LAN connection unavailable'):
            await coordinator._async_update_data()
    recover.assert_awaited_once_with(hass, e)
    await coordinator.async_shutdown()
