"""Automatic credential storage, per-device viewing and local recovery."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect.const import DOMAIN
from custom_components.midea_connect.credential_backup import \
    export_credentials


async def test_restore_uses_only_local_authentication(hass, create_mock_device):
    backup = export_credentials({"id": 1234, "host": "192.0.2.1", "port": 6444,
                                 "device_type": 0xAC, "token": "ab" * 64, "k1": "cd" * 32})
    device = create_mock_device(1234, "192.0.2.99")
    device.authenticate = AsyncMock()
    device.refresh = AsyncMock()
    device.online = True
    device.supported = True
    device.port = 6444
    device.token = "ab" * 64
    device.key = "cd" * 32
    with (
        patch("custom_components.midea_connect.config_flow.Device.construct",
              return_value=device),
        patch("custom_components.midea_connect.config_flow.Discover.connect",
              side_effect=AssertionError("Restore must not use cloud")) as cloud,
        patch("msmart.cloud.BaseCloud._post_request",
              side_effect=AssertionError("No cloud HTTP during restore")) as http,
        patch("custom_components.midea_connect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "restore"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"credentials": backup, "host": "192.0.2.99"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"] == "ab" * 64
    assert result["data"]["k1"] == "cd" * 32
    assert result["data"]["host"] == "192.0.2.99"
    device.authenticate.assert_awaited_once_with("ab" * 64, "cd" * 32)
    cloud.assert_not_called()
    http.assert_not_called()


async def test_invalid_restore_does_not_connect(hass):
    with patch("custom_components.midea_connect.config_flow.Device.construct") as construct:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "restore"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"credentials": "{}"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_credential_backup"}
    construct.assert_not_called()


async def test_restore_timeout_does_not_fall_back_to_cloud(hass, create_mock_device):
    backup = export_credentials({"id": 1234, "host": "192.0.2.1", "port": 6444,
                                 "device_type": 0xAC, "token": "ab" * 64, "k1": "cd" * 32})
    device = create_mock_device(1234, "192.0.2.1")
    device.authenticate = AsyncMock(side_effect=TimeoutError)
    with (
        patch("custom_components.midea_connect.config_flow.Device.construct",
              return_value=device),
        patch("custom_components.midea_connect.config_flow.Discover.connect",
              side_effect=AssertionError("Restore must not use cloud")) as cloud,
        patch("msmart.cloud.BaseCloud._post_request",
              side_effect=AssertionError("No cloud HTTP during restore")) as http,
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "restore"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"credentials": backup})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)
    cloud.assert_not_called()
    http.assert_not_called()


async def test_credentials_available_for_unloaded_entry(hass, mock_config_entry, caplog):
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data={
        **mock_config_entry.data, "token": "ab" * 64, "k1": "cd" * 32,
        "password": "UnrelatedAccountPassword", "account_entry_id": "another-entry"},
        options={"update_interval": 20})
    other = MockConfigEntry(domain=DOMAIN, unique_id="5678", data={
        **mock_config_entry.data, "id": "5678", "token": "ef" * 64, "k1": "12" * 32})
    other.add_to_hass(hass)
    original_data = dict(mock_config_entry.data)
    original_options = dict(mock_config_entry.options)
    with (
        patch("custom_components.midea_connect.config_flow.Discover.connect") as cloud,
        patch("custom_components.midea_connect.config_flow.Device.construct") as construct,
        patch("custom_components.midea_connect.async_setup_entry") as setup,
    ):
        result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == [
            "connection_details", "settings", "show_credentials"]
        assert "ab" * 64 not in repr(result)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "show_credentials"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "show_credentials"
        # Read-only, no editable secrets.
        assert not result["data_schema"].schema
        visible = result["description_placeholders"]["credentials"]
        exported = json.loads(visible)["device"]
        assert exported["id"] == "1234"
        assert exported["token"] == "ab" * 64
        assert exported["key"] == "cd" * 32
        assert "UnrelatedAccountPassword" not in visible
        assert "another-entry" not in visible
        assert "ef" * 64 not in visible
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "credentials_closed"
    assert dict(mock_config_entry.data) == original_data
    assert dict(mock_config_entry.options) == original_options
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert "ab" * 64 not in caplog.text
    assert "cd" * 32 not in caplog.text
    cloud.assert_not_called()
    construct.assert_not_called()
    setup.assert_not_called()


async def test_v2_options_do_not_offer_token_view(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["menu_options"] == ["connection_details", "settings"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_credential_view_does_not_modify_entry(hass, mock_config_entry, cancel):
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data={
        **mock_config_entry.data, "token": "ab" * 64, "k1": "cd" * 32},
        options={"prompt_tone": False})
    original = dict(mock_config_entry.data), dict(mock_config_entry.options)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "show_credentials"})
    if cancel:
        hass.config_entries.options.async_abort(result["flow_id"])
    else:
        await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert (dict(mock_config_entry.data), dict(
        mock_config_entry.options)) == original


async def test_discovery_saves_credentials_without_manual_copy_step(hass, create_mock_device):
    device = create_mock_device(1234, "192.0.2.1")
    device.port = 6444
    device.token = "ab" * 64
    device.key = "cd" * 32
    with (
        patch("custom_components.midea_connect.config_flow.Discover.discover_single",
              return_value=device),
        patch("custom_components.midea_connect.config_flow.Discover.connect",
              return_value=True),
        patch("custom_components.midea_connect.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "discover"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.0.2.1"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "lan_credentials"


async def test_restore_duplicate_keeps_existing_credentials(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    original = dict(mock_config_entry.data)
    backup = export_credentials(original)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "restore"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"credentials": backup})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(mock_config_entry.data) == original
