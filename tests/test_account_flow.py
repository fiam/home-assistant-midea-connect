"""Saved setup accounts and activation use mocked cloud requests."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.midea_connect.const import (CONF_ENTRY_KIND, DOMAIN,
                                                   ENTRY_KIND_ACCOUNT)
from custom_components.midea_connect.nethome_account import (
    AccountError, RegistrationUncertain)

INPUT = {"email": "person@example.com", "password": "Example123",
         "confirm_password": "Example123", "region_code": "62000000", "accept_terms": True}


@pytest.fixture
def account_client():
    client = AsyncMock()
    client.regions.return_value = {"62000000": "Portugal"}
    with patch("custom_components.midea_connect.account_flow.NetHomeAccountClient", return_value=client):
        yield client


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_register"})


async def test_create_and_save_after_activation(hass, account_client):
    result = await start(hass)
    account_client.register.assert_not_called()
    assert "terms_url" in result["description_placeholders"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    account_client.register.assert_awaited_once_with(
        "person@example.com", "Example123", "62000000")
    assert result["step_id"] == "account_activate"
    assert "Example123" not in repr(result)
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    assert "Example123" not in repr(vars(handler))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "Example123"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT,
                              "email": "person@example.com", "password": "Example123",
                              "region_code": "62000000", "account_provider": "nethome"}
    assert handler._account_email is None
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("field,value,error", [
    ("accept_terms", False, "account_accept_terms"),
    ("email", "not-email", "account_invalid_email"),
    ("password", "bad", "account_invalid_password"),
    ("confirm_password", "Different123", "account_password_mismatch"),
])
async def test_invalid_fields_never_register(hass, account_client, field, value, error):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**INPUT, field: value})
    assert result["errors"][field] == error
    account_client.register.assert_not_called()
    assert "Example123" not in repr(result)


async def test_uncertain_registration_not_repeated(hass, account_client):
    account_client.register.side_effect = RegistrationUncertain()
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["step_id"] == "account_activate"
    assert result["errors"] == {"base": "account_registration_uncertain"}
    account_client.check_login.side_effect = AccountError(3103)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "Example123"})
    assert result["errors"] == {"base": "account_not_activated"}
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    await handler.async_step_account_register(INPUT)
    account_client.register.assert_awaited_once()
    account_client.check_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "Example123"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_existing_account_registration_error(hass, account_client):
    account_client.register.side_effect = AccountError(3124)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["errors"] == {"base": "account_already_exists"}
    assert "Example123" not in repr(result)


async def test_metadata_retry_never_registers(hass, account_client):
    account_client.regions.side_effect = [
        AccountError(), {"62000000": "Portugal"}]
    result = await start(hass)
    assert result["step_id"] == "account_prepare"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "account_register"
    account_client.register.assert_not_called()


async def test_cancel_discards_transient_email(hass, account_client):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert handler._account_email is None
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_expired_activation(hass, account_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_activate"})
    assert result["reason"] == "account_setup_expired"
    account_client.check_login.assert_not_called()


async def test_existing_account_save_and_duplicate(hass, account_client):
    for attempt in range(2):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "account_login"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "person@example.com", "password": "Example123"})
        if attempt == 0:
            assert result["type"] is FlowResultType.CREATE_ENTRY
        else:
            assert result["reason"] == "already_configured"
    account_client.check_login.assert_awaited_once()
    account_client.register.assert_not_called()
