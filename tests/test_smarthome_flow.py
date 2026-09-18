"""SmartHome signup retries, provider isolation and transient-secret handling."""
from hashlib import sha256
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect.const import (CONF_ACCOUNT_PROVIDER,
                                                   CONF_ENTRY_KIND, DOMAIN,
                                                   ENTRY_KIND_ACCOUNT,
                                                   PROVIDER_SMARTHOME)
from custom_components.midea_connect.nethome_account import (
    AccountError, RegistrationUncertain)

DETAILS = {'email': 'person@example.com',
           'region_code': 'PT', 'accept_terms': True}
PASSWORD = {'password': 'Example123', 'confirm_password': 'Example123'}


@pytest.fixture
def cloud():
    client = AsyncMock()
    client.regions.return_value = {'PT': 'Portugal'}
    client.verify_code.return_value = 'verification-proof'
    with patch('custom_components.midea_connect.smarthome_flow.SmartHomeAccountClient', return_value=client):
        yield client


async def start(hass, source='smarthome_register'):
    return await hass.config_entries.flow.async_init(DOMAIN, context={'source': source})


async def to_password(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    return await hass.config_entries.flow.async_configure(result['flow_id'], {'verification_code': '123456'})


async def test_create_verify_save_and_clear(hass, cloud):
    result = await start(hass)
    cloud.send_code.assert_not_called()
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    assert result['step_id'] == 'smarthome_verify'
    cloud.prepare_registration.assert_awaited_once_with(
        'person@example.com', 'PT')
    cloud.send_code.assert_awaited_once_with('person@example.com')
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'verification_code': '123456'})
    cloud.register.assert_not_called()
    assert result['step_id'] == 'smarthome_password'
    handler = hass.config_entries.flow._progress[result['flow_id']]
    result = await hass.config_entries.flow.async_configure(result['flow_id'], PASSWORD)
    assert result['type'] is FlowResultType.CREATE_ENTRY
    assert result['data'] == {CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT, CONF_ACCOUNT_PROVIDER: PROVIDER_SMARTHOME,
                              'email': 'person@example.com', 'password': 'Example123', 'region_code': 'PT'}
    cloud.register.assert_awaited_once_with(
        'person@example.com', 'Example123', 'PT', 'verification-proof')
    cloud.check_login.assert_awaited_once_with(
        'person@example.com', 'Example123')
    assert handler._smarthome_client is handler._smarthome_random_code is handler._account_email is None
    await hass.async_block_till_done()


async def test_terms_required_before_code(hass, cloud):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {**DETAILS, 'accept_terms': False})
    assert result['errors']['accept_terms'] == 'account_accept_terms'
    cloud.prepare_registration.assert_not_called()
    cloud.send_code.assert_not_called()


async def test_uncertain_send_and_explicit_resend_cooldown(hass, cloud):
    cloud.send_code.side_effect = RegistrationUncertain()
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    assert result['errors']['base'] == 'smarthome_code_send_uncertain'
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'resend_code': True})
    assert result['errors']['base'] == 'smarthome_resend_wait'
    cloud.send_code.assert_awaited_once()
    handler = hass.config_entries.flow._progress[result['flow_id']]
    handler._smarthome_next_send = 0
    cloud.send_code.side_effect = None
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'resend_code': True})
    assert not result['errors']
    assert cloud.send_code.await_count == 2


async def test_uncertain_registration_only_retries_login(hass, cloud):
    result = await to_password(hass)
    cloud.register.side_effect = RegistrationUncertain()
    result = await hass.config_entries.flow.async_configure(result['flow_id'], PASSWORD)
    assert result['step_id'] == 'smarthome_finish'
    assert result['errors']['base'] == 'smarthome_registration_uncertain'
    handler = hass.config_entries.flow._progress[result['flow_id']]
    assert handler._smarthome_random_code is None
    assert 'Example123' not in repr(result)+repr(vars(handler))
    cloud.check_login.side_effect = AccountError(3102)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'password': 'Example123'})
    assert result['errors']['base'] == 'smarthome_finish_login_failed'
    await handler.async_step_smarthome_password(PASSWORD)
    await handler.async_step_smarthome_register(DETAILS)
    cloud.register.assert_awaited_once()
    cloud.check_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'password': 'Example123'})
    assert result['type'] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_invalid_code_and_password_never_register(hass, cloud):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'verification_code': 'abc'})
    assert result['errors']['verification_code'] == 'smarthome_invalid_code'
    cloud.verify_code.assert_not_called()
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'verification_code': '123456'})
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'password': 'short', 'confirm_password': 'different'})
    assert result['errors']['password'] == 'smarthome_invalid_password'
    assert result['errors']['confirm_password'] == 'account_password_mismatch'
    cloud.register.assert_not_called()


async def test_cancel_clears_verification_proof(hass, cloud):
    result = await to_password(hass)
    handler = hass.config_entries.flow._progress[result['flow_id']]
    hass.config_entries.flow.async_abort(result['flow_id'])
    assert handler._smarthome_random_code is handler._smarthome_client is handler._account_email is None
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_existing_login_provider_uniqueness(hass, cloud):
    digest = sha256(DETAILS['email'].encode()).hexdigest()
    MockConfigEntry(domain=DOMAIN, unique_id='nethome-account-'+digest,
                    data={CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT}).add_to_hass(hass)
    for attempt in range(2):
        result = await start(hass, 'smarthome_login')
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'email': DETAILS['email'], 'password': 'Example123'})
        if not attempt:
            assert result['type'] is FlowResultType.CREATE_ENTRY
            assert result['data'][CONF_ACCOUNT_PROVIDER] == PROVIDER_SMARTHOME
        else:
            assert result['reason'] == 'already_configured'
    cloud.check_login.assert_awaited_once()
    cloud.register.assert_not_called()
    await hass.async_block_till_done()


async def test_failed_send_retry_respects_cooldown(hass, cloud):
    cloud.send_code.side_effect = AccountError(9999)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    assert result['errors']['base'] == 'smarthome_code_send_failed'
    result = await hass.config_entries.flow.async_configure(result['flow_id'], DETAILS)
    assert result['errors']['base'] == 'smarthome_resend_wait'
    cloud.send_code.assert_awaited_once()


async def test_provider_menus(hass, cloud):
    result = await start(hass, 'account')
    assert result['menu_options'] == ['smarthome_account', 'nethome_account']
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'next_step_id': 'smarthome_account'})
    assert result['menu_options'] == ['smarthome_register', 'smarthome_login']
