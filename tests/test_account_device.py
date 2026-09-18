"""Credential acquisition, persistence and offline account restore."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from msmart.const import DeviceType
from msmart.lan import Security
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect import (async_setup_entry,
                                             async_unload_entry)
from custom_components.midea_connect.account_device import (
    DeviceSetupError, acquire_credentials)
from custom_components.midea_connect.const import (CONF_ACCOUNT_ENTRY_ID,
                                                   CONF_ENTRY_KIND, CONF_KEY,
                                                   DOMAIN, ENTRY_KIND_ACCOUNT)
from custom_components.midea_connect.diagnostics import \
    async_get_config_entry_diagnostics
from custom_components.midea_connect.nethome_account import AccountError


@pytest.fixture
def setup_account(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="nethome-account-fixture",
                            version=1, minor_version=7,
                            data={CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT,
                                  "email": "fixture@example.com", "password": "Private123",
                                  "region_code": "62000000"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def device():
    dev = MagicMock()
    dev.id = 123456789
    dev.ip = "192.0.2.10"
    dev.port = 6444
    dev.version = 3
    dev.type = DeviceType.AIR_CONDITIONER
    dev.token = "11" * 64
    dev.key = "22" * 32
    dev.online = dev.supported = True
    dev.authenticate = AsyncMock()
    dev.refresh = AsyncMock()
    return dev


async def test_saved_account_startup_is_offline_and_diagnostics_redacted(hass, setup_account):
    with patch("custom_components.midea_connect.Device.construct") as construct, \
            patch("custom_components.midea_connect.nethome_account.NetHomeAccountClient.check_login",
                  side_effect=AssertionError("Startup must be offline")):
        assert await async_setup_entry(hass, setup_account)
        assert await async_unload_entry(hass, setup_account)
        construct.assert_not_called()
    diagnostic = await async_get_config_entry_diagnostics(hass, setup_account)
    assert diagnostic == {"entry_kind": ENTRY_KIND_ACCOUNT, "region_code": "62000000",
                          "account_provider": "nethome",
                          "credentials_saved": True}
    assert "Private123" not in repr(diagnostic)
    assert "fixture@example.com" not in repr(diagnostic)


async def test_account_has_no_device_credential_options(hass, setup_account):
    result = await hass.config_entries.options.async_init(setup_account.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_options_reconfigure"


async def test_token_acquisition_tries_alternate_after_cloud_error(device, setup_account):
    cloud = AsyncMock()
    cloud.get_token.side_effect = [
        AccountError(9999), (device.token, device.key)]
    with patch("custom_components.midea_connect.account_device.NetHomeAccountClient", return_value=cloud), \
            patch("custom_components.midea_connect.account_device.Discover.discover_single",
                  return_value=device) as discover:
        result = await acquire_credentials(MagicMock(), setup_account.data, device.ip)
    assert result is device
    discover.assert_awaited_once_with(device.ip, auto_connect=False, timeout=3)
    cloud.check_login.assert_awaited_once_with(
        "fixture@example.com", "Private123")
    assert [call.args[0] for call in cloud.get_token.await_args_list] == [
        Security.udpid(device.id.to_bytes(6, order)).hex() for order in ("big", "little")]
    device.authenticate.assert_awaited_once_with(device.token, device.key)
    device.refresh.assert_awaited_once()
    device.apply.assert_not_called()
    device._lan._disconnect.assert_called_once()


async def test_cloud_failure_cannot_authenticate_or_change_device(device, setup_account):
    cloud = AsyncMock()
    cloud.get_token.side_effect = AccountError(9999)
    with patch("custom_components.midea_connect.account_device.NetHomeAccountClient", return_value=cloud), \
            patch("custom_components.midea_connect.account_device.Discover.discover_single", return_value=device):
        with pytest.raises(DeviceSetupError, match="account_token_unavailable"):
            await acquire_credentials(MagicMock(), setup_account.data, device.ip)
    device.authenticate.assert_not_called()
    device.refresh.assert_not_called()
    device.apply.assert_not_called()


async def test_new_device_saves_only_lan_secrets_and_account_reference(hass, setup_account, device):
    with patch("custom_components.midea_connect.config_flow.acquire_credentials", return_value=device), \
            patch("custom_components.midea_connect.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_device"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": "nethome"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_ACCOUNT_ENTRY_ID: setup_account.entry_id, "host": device.ip})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["token"] == device.token
        assert result["data"][CONF_KEY] == device.key
        assert result["data"][CONF_ACCOUNT_ENTRY_ID] == setup_account.entry_id
        assert "password" not in result["data"]
        assert "email" not in result["data"]
        await hass.async_block_till_done()


async def test_failed_refresh_preserves_old_credentials(hass, setup_account, device):
    old = MockConfigEntry(domain=DOMAIN, unique_id=str(device.id), data={
        "id": device.id, "device_type": 0xAC, "host": device.ip, "port": 6444,
        "token": "33" * 64, CONF_KEY: "44" * 32})
    old.add_to_hass(hass)
    before = dict(old.data)
    with patch("custom_components.midea_connect.config_flow.acquire_credentials",
               side_effect=DeviceSetupError("account_token_unavailable")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_device"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": "nethome"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_ACCOUNT_ENTRY_ID: setup_account.entry_id, "host": device.ip})
    assert result["errors"] == {"base": "account_token_unavailable"}
    assert dict(old.data) == before


async def test_successful_refresh_updates_existing_entry(hass, setup_account, device):
    old = MockConfigEntry(domain=DOMAIN, unique_id=str(device.id), version=1, minor_version=7, data={
        "id": device.id, "device_type": 0xAC, "host": device.ip, "port": 6444,
        "token": "33" * 64, CONF_KEY: "44" * 32}, options={"update_interval": 20})
    old.add_to_hass(hass)
    with patch("custom_components.midea_connect.config_flow.acquire_credentials", return_value=device), \
            patch("custom_components.midea_connect.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_device"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": "nethome"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_ACCOUNT_ENTRY_ID: setup_account.entry_id, "host": device.ip})
        assert result["reason"] == "account_tokens_updated"
        assert old.data["token"] == device.token
        assert old.data[CONF_KEY] == device.key
        assert old.options == {"update_interval": 20}
        assert len(hass.config_entries.async_entries(DOMAIN)) == 2
        await hass.async_block_till_done()


async def test_account_password_reconfiguration(hass, setup_account):
    with patch("custom_components.midea_connect.account_flow.NetHomeAccountClient") as cls:
        cls.return_value.check_login = AsyncMock()
        result = await hass.config_entries.flow.async_init(DOMAIN, context={
            "source": "reconfigure", "entry_id": setup_account.entry_id})
        assert result["step_id"] == "account_password"
        assert "Private123" not in repr(result)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "Updated123"})
        assert result["reason"] == "reconfigure_successful"
        assert setup_account.data["password"] == "Updated123"
        await hass.async_block_till_done()


async def test_smarthome_uses_matched_cloud_id_and_verifies_lan(device, setup_account):
    cloud = AsyncMock()
    cloud.close = MagicMock()
    cloud.appliance_id.return_value = '998877'
    cloud.get_token.return_value = (device.token, device.key)
    account = {**setup_account.data, 'account_provider': 'smarthome'}
    with patch('custom_components.midea_connect.account_device.SmartHomeAccountClient', return_value=cloud), \
            patch('custom_components.midea_connect.account_device.NetHomeAccountClient') as nethome, \
            patch('custom_components.midea_connect.account_device.Discover.discover_single', return_value=device):
        assert await acquire_credentials(MagicMock(), account, device.ip) is device
    nethome.assert_not_called()
    cloud.appliance_id.assert_awaited_once_with(device)
    cloud.get_token.assert_awaited_once_with(
        Security.udpid(device.id.to_bytes(6, 'big')).hex(), '998877')
    device.authenticate.assert_awaited_once_with(device.token, device.key)
    device.apply.assert_not_called()


async def test_smarthome_reconfigure_and_offline_restore(hass, setup_account):
    hass.config_entries.async_update_entry(
        setup_account, data={**setup_account.data, 'account_provider': 'smarthome'})
    with patch('custom_components.midea_connect.account_flow.SmartHomeAccountClient') as cls, \
            patch('custom_components.midea_connect.account_flow.NetHomeAccountClient') as nethome:
        cls.return_value.check_login = AsyncMock()
        assert await async_setup_entry(hass, setup_account)
        cls.assert_not_called()
        result = await hass.config_entries.flow.async_init(DOMAIN, context={
            'source': 'reconfigure', 'entry_id': setup_account.entry_id})
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'password': 'Updated123'})
        assert result['reason'] == 'reconfigure_successful'
        cls.return_value.check_login.assert_awaited_once_with(
            'fixture@example.com', 'Updated123')
        nethome.assert_not_called()
        await hass.async_block_till_done()
