"""User-facing routing and provider isolation for Midea Connect."""
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect.const import DOMAIN, ENTRY_KIND_ACCOUNT

from .test_bluetooth_discovery import advertisement


def account(hass, provider):
    entry = MockConfigEntry(domain=DOMAIN, data={"entry_kind": ENTRY_KIND_ACCOUNT,
                                                 "account_provider": provider, "email": f"{provider}@example.com", "password": "Private123"})
    entry.add_to_hass(hass)
    return entry


async def test_provider_selection_filters_accounts(hass):
    smart, net = account(hass, "smarthome"), account(hass, "nethome")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_device"})
    assert result["step_id"] == "setup_provider"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": "smarthome"})
    options = result["data_schema"].schema["account_entry_id"].container
    assert set(options) == {smart.entry_id}
    assert net.entry_id not in options
    assert "Private123" not in repr(result)


async def test_missing_account_can_be_added_without_losing_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_device"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": "nethome"})
    assert result["step_id"] == "setup_account_missing"
    entry = account(hass, "nethome")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "account_device"
    assert entry.entry_id in result["data_schema"].schema["account_entry_id"].container


async def test_nearby_filters_configured_and_stale_and_merges_scanners(hass):
    configured = advertisement(address="configured")
    entry = MockConfigEntry(domain=DOMAIN, data={
                            "bluetooth_address": "configured"})
    entry.add_to_hass(hass)
    seen = [advertisement(rssi=-90), advertisement(rssi=-45),
            configured, advertisement(address="stale")]
    with (patch("custom_components.midea_connect.config_flow.bluetooth.async_scanner_count", return_value=1),
          patch("custom_components.midea_connect.config_flow.bluetooth.async_discovered_service_info", return_value=seen),
          patch("custom_components.midea_connect.config_flow.bluetooth.async_address_present",
                side_effect=lambda hass, address, **kw: address != "stale"),
          patch("custom_components.midea_connect.config_flow.bluetooth.async_scanner_devices_by_address", return_value=[])):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "nearby_bluetooth"})
        labels = result["data_schema"].schema["address"].container
        assert len(labels) == 4  # One AC and three navigation actions.
        assert "-45 dBm" in labels[seen[0].address]
        assert "No Bluetooth connection route" in labels[seen[0].address]
        seen.clear()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": "refresh"})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]["base"] == "no_bluetooth_devices"
        assert "discover" in result["data_schema"].schema["address"].container


async def test_lan_discovery_never_uses_implicit_cloud_login(hass, create_mock_device):
    device = create_mock_device(1234, "192.0.2.1")
    with (patch("custom_components.midea_connect.config_flow.Discover.discover_single", return_value=device),
          patch("custom_components.midea_connect.config_flow.Discover.connect") as connect):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "discover"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": device.ip})
    assert result["step_id"] == "lan_credentials"
    assert result["menu_options"] == ["account_device", "restore", "manual"]
    connect.assert_not_called()


async def test_connection_details_for_unloaded_device_are_read_only(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={
                            "host": "192.0.2.1", "token": "ab" * 64, "k1": "cd" * 32})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "connection_details"})
    assert result["description_placeholders"]["host"] == "192.0.2.1"
    assert result["description_placeholders"]["last_seen"] == "Not yet verified"
    assert "ab" * 64 not in repr(result)
    assert not result["data_schema"].schema


async def test_provider_account_menus_list_only_their_accounts(hass):
    account(hass, "smarthome")
    account(hass, "nethome")
    for step, provider in [("smarthome_account", "smarthome"), ("nethome_account", "nethome")]:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": step})
        names = result["description_placeholders"]["accounts"]
        assert f"{provider}@example.com" in names
        assert ("nethome" if provider == "smarthome" else "smarthome") + \
            "@example.com" not in names


async def test_configured_serial_suppresses_new_bluetooth_discovery(hass):
    from .test_bluetooth_transport import SERIAL
    entry = MockConfigEntry(domain=DOMAIN, data={"sn": SERIAL})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=advertisement())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_translated_menus_have_handlers_and_progress_captions():
    import json
    from pathlib import Path

    from custom_components.midea_connect.config_flow import (MideaConfigFlow,
                                                             MideaOptionsFlow)
    for path in Path('custom_components/midea_connect/translations').glob('*.json'):
        data = json.loads(path.read_text())
        for section, handler in [('config', MideaConfigFlow), ('options', MideaOptionsFlow)]:
            for step in data[section]['step'].values():
                for action in step.get('menu_options', {}):
                    assert hasattr(handler, 'async_step_' +
                                   action), (path, action)
        for stage in ('cloud_login', 'bluetooth_wifi', 'cloud_link', 'lan_search', 'cloud_credentials', 'lan_verified'):
            assert stage in data['config']['progress']
