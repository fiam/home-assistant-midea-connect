"""Provisioning retries must not resend Wi-Fi settings or bind a different AC."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.midea_connect.account_device import DeviceSetupError
from custom_components.midea_connect.bluetooth_provision import (
    ProvisioningState, WifiCredentials, find_bluetooth_device_on_lan,
    find_on_lan, provision_device)
from custom_components.midea_connect.bluetooth_transport import \
    BluetoothSetupError
from custom_components.midea_connect.nethome_account import AccountError

from .test_bluetooth_transport import DISCOVERY, SERIAL

ACCOUNT = {"email": "person@example.com", "password": "Account123"}


@pytest.fixture
def setup_services():
    cloud = AsyncMock()
    binding = AsyncMock()
    binding.wait_for_device.return_value = ("1234", "one-time-proof")
    # close is synchronous.
    binding.close = lambda: None
    session = AsyncMock()
    session.device_info.return_value = SimpleNamespace(serial=SERIAL)

    async def configure(_payload, mark_started):
        mark_started()
        return 0
    session.configure_wifi.side_effect = configure
    session.__aenter__.return_value = session
    device = SimpleNamespace(ip="192.0.2.10", sn=SERIAL)
    with (
        patch("custom_components.midea_connect.bluetooth_provision.NetHomeAccountClient",
              return_value=cloud),
        patch("custom_components.midea_connect.bluetooth_provision.NetHomeBindingClient",
              return_value=binding),
        patch("custom_components.midea_connect.bluetooth_provision.MideaBluetoothSession", return_value=session) as connect,
        patch("custom_components.midea_connect.bluetooth_provision.find_on_lan",
              return_value=device),
        patch("custom_components.midea_connect.bluetooth_provision.acquire_credentials", return_value=device) as acquire,
    ):
        yield cloud, binding, session, connect, acquire, device


async def test_success_verifies_exact_serial_and_discards_wifi(hass, setup_services):
    cloud, binding, session, connect, acquire, device = setup_services
    state = ProvisioningState(serial=SERIAL)
    wifi = WifiCredentials("Home", "WifiPrivate123")
    assert await provision_device(hass, None, DISCOVERY, ACCOUNT, state, wifi) is device
    assert state.write_started and state.wifi_acknowledged and state.bound
    assert not state.random_code
    assert wifi.ssid == wifi.password == ""
    binding.bind.assert_awaited_once_with(
        "1234", "one-time-proof", "AC 7E4D", appliance_type="AC")
    acquire.assert_awaited_once_with(
        None, ACCOUNT, "192.0.2.10", expected_serial=SERIAL)


async def test_cloud_login_failure_never_writes_wifi(hass, setup_services):
    cloud, _, session, connect, _, _ = setup_services
    cloud.check_login.side_effect = AccountError()
    state = ProvisioningState()
    wifi = WifiCredentials("Home", "WifiPrivate123")
    with pytest.raises(BluetoothSetupError, match="account_login_failed"):
        await provision_device(hass, None, DISCOVERY, ACCOUNT, state, wifi)
    connect.assert_not_called()
    assert not state.write_started and not wifi.password


async def test_lost_ack_does_not_repeat_wifi_on_retry(hass, setup_services):
    _, binding, session, connect, acquire, device = setup_services

    async def uncertain(_payload, mark_started):
        mark_started()
        raise TimeoutError()
    session.configure_wifi.side_effect = uncertain
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match="ble_setup_incomplete"):
        await provision_device(hass, None, DISCOVERY, ACCOUNT, state, WifiCredentials("Home", "WifiPrivate123"))
    assert state.write_started and not state.wifi_acknowledged and len(
        state.random_code) == 16
    assert await provision_device(hass, None, DISCOVERY, ACCOUNT, state) is device
    assert connect.call_count == 1
    session.configure_wifi.assert_awaited_once()


async def test_token_failure_after_binding_retries_only_credentials(hass, setup_services):
    _, binding, session, connect, acquire, device = setup_services
    acquire.side_effect = [DeviceSetupError(
        "account_token_unavailable"), device]
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(DeviceSetupError):
        await provision_device(hass, None, DISCOVERY, ACCOUNT, state, WifiCredentials("Home", "WifiPrivate123"))
    assert state.bound
    assert await provision_device(hass, None, DISCOVERY, ACCOUNT, state) is device
    assert connect.call_count == 1
    binding.bind.assert_awaited_once()
    binding.prepare.assert_awaited_once()


async def test_wrong_wifi_password_allows_explicit_new_credentials(hass, setup_services):
    _, binding, session, _, acquire, _ = setup_services
    session.wait_for_network.side_effect = BluetoothSetupError(
        "ble_wifi_wrong_password")
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match="ble_wifi_wrong_password"):
        await provision_device(hass, None, DISCOVERY, ACCOUNT, state, WifiCredentials("Home", "WifiPrivate123"))
    assert not state.write_started
    binding.bind.assert_not_called()
    acquire.assert_not_called()


@pytest.mark.parametrize("code,reason", [(123, "ble_binding_rejected"), (None, "ble_binding_failed")])
async def test_cloud_binding_failure_never_saves_lan_credentials(hass, setup_services, code, reason):
    _, binding, _, _, acquire, _ = setup_services
    binding.bind.side_effect = AccountError(code)
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match=reason) as exc:
        await provision_device(hass, None, DISCOVERY, ACCOUNT, state, WifiCredentials("Home", "WifiPrivate123"))
    assert exc.value.cloud_code == code
    assert state.write_started and not state.bound
    acquire.assert_not_called()


async def test_lan_discovery_ignores_other_ac_serial():
    other = SimpleNamespace(sn="another-controller")
    target = SimpleNamespace(sn=SERIAL)
    with patch("custom_components.midea_connect.bluetooth_provision.Discover.discover",
               side_effect=[[other], [other, target]]) as discover, \
            patch("custom_components.midea_connect.bluetooth_provision.asyncio.sleep", new=AsyncMock()):
        assert await find_on_lan(SERIAL) is target
    assert discover.await_count == 2


async def test_advertisement_lan_match_requires_model_identity_and_unique_ac():
    target = SimpleNamespace(sn=SERIAL, type=0xAC)
    # Same suffix but another serial family is not the advertised controller.
    other = SimpleNamespace(sn=SERIAL.replace("Q1B", "Q15"), type=0xAC)
    with patch("custom_components.midea_connect.bluetooth_provision.Discover.discover",
               side_effect=[[other], [other, target], [target, target]]) as discover:
        assert await find_bluetooth_device_on_lan(DISCOVERY) is None
        assert await find_bluetooth_device_on_lan(DISCOVERY) is target
        assert await find_bluetooth_device_on_lan(DISCOVERY) is None
    assert all(call.kwargs["auto_connect"]
               is False for call in discover.call_args_list)


async def test_unknown_provider_never_writes_wifi_or_uses_nethome(hass, setup_services):
    cloud, binding, session, connect, acquire, _ = setup_services
    wifi = WifiCredentials('Home', 'WifiPrivate123')
    state = ProvisioningState()
    with pytest.raises(BluetoothSetupError, match='account_login_failed'):
        await provision_device(hass, None, DISCOVERY, {**ACCOUNT, 'account_provider': 'unknown'}, state, wifi)
    cloud.check_login.assert_not_called()
    connect.assert_not_called()
    binding.bind.assert_not_called()
    acquire.assert_not_called()
    assert not state.write_started and not wifi.password


SMART_ACCOUNT = {**ACCOUNT, 'account_provider': 'smarthome'}


@pytest.fixture
def smart_services(setup_services):
    cloud = AsyncMock()
    cloud.close = Mock()
    binding = AsyncMock()
    binding.close = Mock()
    binding.owned_id.side_effect = [None, None, '998877']
    binding.wait_for_device.return_value = {'model_number': '4321'}
    binding.bind.return_value = '998877'
    binding.confirmation_status.return_value = 0
    binding.confirmation_instructions.return_value = 'Press the pairing button.'
    with patch('custom_components.midea_connect.bluetooth_provision.SmartHomeAccountClient', return_value=cloud), \
            patch('custom_components.midea_connect.bluetooth_provision.SmartHomeBindingClient', return_value=binding):
        yield cloud, binding, setup_services


async def test_smarthome_links_confirms_and_acquires_locally(hass, smart_services):
    cloud, binding, (nethome, oem, session, connect,
                     acquire, device) = smart_services
    state = ProvisioningState(serial=SERIAL)
    wifi = WifiCredentials('Home', 'Private123')
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, wifi) is device
    assert state.bound and state.appliance_code == '998877' and not state.random_code
    assert session.configure_wifi.call_args.args[0][0] == 1
    binding.bind.assert_awaited_once_with(
        SERIAL, 'AC 7E4D', 'AC', model_number='4321')
    binding.start_confirmation.assert_not_called()
    acquire.assert_awaited_once_with(
        None, SMART_ACCOUNT, device.ip, expected_serial=SERIAL)
    nethome.check_login.assert_not_called()
    oem.bind.assert_not_called()
    assert not wifi.password


async def test_smarthome_lost_bind_reply_reconciles_without_another_write(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, device) = smart_services
    binding.bind.side_effect = AccountError()
    binding.owned_id.side_effect = [None, None, '998877', '998877']
    state = ProvisioningState(serial=SERIAL)
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                  WifiCredentials('Home', 'Private123')) is device
    binding.bind.assert_awaited_once()
    assert state.bound


async def test_smarthome_pending_confirmation_retries_only_status_and_tokens(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, device) = smart_services
    binding.owned_id.side_effect = [None, None, '998877', '998877']
    binding.confirmation_status.side_effect = [1, 2, 3]
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match='smarthome_confirmation_required'):
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, WifiCredentials('Home', 'Private123'))
    assert state.appliance_code == '998877' and not state.bound
    assert state.confirmation_started
    acquire.assert_not_called()
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state) is device
    assert connect.call_count == 1
    binding.bind.assert_awaited_once()
    binding.start_confirmation.assert_awaited_once()
    assert state.bound and not state.confirmation_instructions


async def test_smarthome_lost_confirmation_reply_does_not_repeat_it(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, device) = smart_services
    binding.owned_id.side_effect = [None, None, '998877', '998877']
    binding.confirmation_status.side_effect = [1, 0]
    binding.start_confirmation.side_effect = AccountError()
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match='smarthome_confirmation_failed'):
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, WifiCredentials('Home', 'Private123'))
    assert state.confirmation_started
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state) is device
    binding.start_confirmation.assert_awaited_once()
    binding.bind.assert_awaited_once()


async def test_smarthome_rejects_bind_without_matching_ownership(hass, smart_services):
    _, binding, (_, _, _, _, acquire, _) = smart_services
    binding.owned_id.side_effect = [None, None, '667788']
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match='smarthome_ownership_pending'):
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, WifiCredentials('Home', 'Private123'))
    assert not state.bound
    acquire.assert_not_called()


async def test_smarthome_token_failure_does_not_repeat_binding(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, device) = smart_services
    acquire.side_effect = [DeviceSetupError(
        'account_token_unavailable'), device]
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(DeviceSetupError):
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, WifiCredentials('Home', 'Private123'))
    assert state.bound
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state) is device
    binding.bind.assert_awaited_once()
    assert connect.call_count == 1


async def test_smarthome_rejected_preflight_never_sends_wifi(hass, smart_services):
    _, binding, (_, _, _, connect, acquire, _) = smart_services
    binding.owned_id.side_effect = AccountError(3004)
    wifi = WifiCredentials('Home', 'Private123')
    with pytest.raises(BluetoothSetupError, match='account_login_failed'):
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, ProvisioningState(serial=SERIAL), wifi)
    assert not wifi.password
    connect.assert_not_called()
    acquire.assert_not_called()


async def test_smarthome_binds_before_silent_ble_status_times_out(hass, smart_services):
    _, binding, (_, _, session, _, _, device) = smart_services
    listening, stopped = asyncio.Event(), asyncio.Event()

    async def network(**kwargs):
        listening.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def lookup(*args):
        await listening.wait()
        return {'model_number': '4321'}

    async def bind(*args, **kwargs):
        assert stopped.is_set()  # No lingering status reader before mutation.
        session.__aexit__.assert_not_awaited()  # Nor a disconnect delay.
        return '998877'

    session.wait_for_network.side_effect = network
    binding.wait_for_device.side_effect = lookup
    binding.bind.side_effect = bind
    state = ProvisioningState(serial=SERIAL)
    async with asyncio.timeout(1):
        assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                      WifiCredentials('Home', 'Private123')) is device
    assert state.bound and stopped.is_set()
    session.configure_wifi.assert_awaited_once()
    binding.bind.assert_awaited_once()


@pytest.mark.parametrize('reason', ['ble_wifi_wrong_password', 'ble_wifi_not_found'])
async def test_smarthome_wifi_error_cancels_cloud_lookup_before_binding(hass, smart_services, reason):
    _, binding, (_, _, session, _, acquire, _) = smart_services
    started, stopped = asyncio.Event(), asyncio.Event()

    async def lookup(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def network(**kwargs):
        await started.wait()
        raise BluetoothSetupError(reason)

    binding.wait_for_device.side_effect = lookup
    session.wait_for_network.side_effect = network
    state = ProvisioningState(serial=SERIAL)
    async with asyncio.timeout(1):
        with pytest.raises(BluetoothSetupError, match=reason):
            await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                   WifiCredentials('Home', 'Private123'))
    assert stopped.is_set() and not state.write_started
    binding.bind.assert_not_called()
    acquire.assert_not_called()


async def test_smarthome_cancellation_joins_both_readers(hass, smart_services):
    _, binding, (_, _, session, _, acquire, _) = smart_services
    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    async def read(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped[index].set()

    async def lookup(*args):
        await read(0)

    async def network(**kwargs):
        await read(1)

    binding.wait_for_device.side_effect = lookup
    session.wait_for_network.side_effect = network
    state = ProvisioningState(serial=SERIAL)
    task = asyncio.create_task(provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                                WifiCredentials('Home', 'Private123')))
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(event.wait() for event in started))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(event.is_set() for event in stopped)
    binding.bind.assert_not_called()
    acquire.assert_not_called()
    binding.close.assert_called_once()
    session.__aexit__.assert_awaited_once()


async def test_smarthome_expired_binding_does_not_retry_old_proof(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, _) = smart_services
    binding.owned_id.side_effect = None
    binding.owned_id.return_value = None
    binding.bind.side_effect = AccountError(1383)
    state = ProvisioningState(serial=SERIAL)
    for wifi in (WifiCredentials('Home', 'Private123'), None):
        with pytest.raises(BluetoothSetupError, match='smarthome_pairing_expired') as exc:
            await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, wifi)
        assert exc.value.cloud_code == 1383
    assert state.binding_expired and state.write_started and not state.bound
    assert not state.random_code
    binding.wait_for_device.assert_awaited_once()
    binding.bind.assert_awaited_once()
    session.configure_wifi.assert_awaited_once()
    acquire.assert_not_called()


async def test_failed_pairing_trace_measures_since_wifi_and_excludes_secrets(hass, smart_services, caplog):
    cloud, binding, (_, _, session, _, _, _) = smart_services
    clock = [1000.0]
    state = ProvisioningState(serial=SERIAL)
    proofs = []

    async def login(*args):
        # Slow login before the device receives any Wi-Fi write.
        clock[0] += 80

    async def configure(_payload, mark_started):
        mark_started()
        clock[0] += 0.25
        return 3  # Existing Wi-Fi must be distinguished from a new ACK 0.

    async def lookup(*args):
        proofs.append(state.random_code.hex())
        clock[0] += 0.5
        return {'model_number': '4321'}

    async def bind(*args, **kwargs):
        clock[0] += 0.1
        raise AccountError(1383)

    cloud.check_login.side_effect = login
    session.configure_wifi.side_effect = configure
    binding.wait_for_device.side_effect = lookup
    binding.bind.side_effect = bind
    binding.owned_id.side_effect = None
    binding.owned_id.return_value = None
    with patch('custom_components.midea_connect.bluetooth_provision.monotonic', side_effect=lambda: clock[0]):
        with pytest.raises(BluetoothSetupError, match='smarthome_pairing_expired'):
            await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                   WifiCredentials('PrivateSSID', 'PrivatePassword'))
    record = next(r for r in caplog.records if r.msg ==
                  'SmartHome setup trace: %s')
    trace = json.loads(record.args[0])
    events = {event['stage']: event for event in trace['events']}
    assert events['wifi_ack']['code'] == 3
    assert events['wifi_ack']['since_wifi_write_ms'] == 250
    assert events['bind_started']['elapsed_ms'] == 80750
    assert events['bind_started']['since_wifi_write_ms'] == 750
    assert events['bind_rejected']['code'] == 1383
    assert events['bind_rejected']['since_wifi_write_ms'] == 850
    for secret in [SERIAL, DISCOVERY.address, ACCOUNT['email'], ACCOUNT['password'],
                   'PrivateSSID', 'PrivatePassword', *proofs]:
        assert secret not in record.getMessage()


async def test_smarthome_offline_confirmation_preserves_link_and_retries_without_writes(hass, smart_services):
    _, binding, (_, _, session, connect, acquire, device) = smart_services
    binding.owned_id.side_effect = [None, None, '998877', '998877']
    binding.confirmation_status.side_effect = [AccountError(3123), 0]
    state = ProvisioningState(serial=SERIAL)
    with pytest.raises(BluetoothSetupError, match='smarthome_device_offline') as error:
        await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state, WifiCredentials('Home', 'Private123'))
    assert error.value.cloud_code == 3123
    assert state.write_started and state.appliance_code == '998877'
    assert not state.bound
    acquire.assert_not_called()
    assert state.trace.events[-2]['stage'] == 'confirmation_check_failed'
    assert state.trace.events[-2]['code'] == 3123
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state) is device
    assert state.bound
    session.configure_wifi.assert_awaited_once()
    binding.bind.assert_awaited_once()
    binding.start_confirmation.assert_not_called()


async def test_already_owned_ac_waits_for_network_before_confirmation(hass, smart_services):
    _, binding, (_, _, session, _, _, device) = smart_services
    binding.owned_id.side_effect = None
    binding.owned_id.return_value = '998877'
    network_ready = False

    async def wait_network(**kwargs):
        nonlocal network_ready
        network_ready = True

    async def confirmation(code):
        assert network_ready, 'Ownership must not bypass the Wi-Fi join wait'
        return 0

    session.wait_for_network.side_effect = wait_network
    binding.confirmation_status.side_effect = confirmation
    state = ProvisioningState(serial=SERIAL)
    assert await provision_device(hass, None, DISCOVERY, SMART_ACCOUNT, state,
                                  WifiCredentials('Home', 'Private123')) is device
    session.wait_for_network.assert_awaited_once()
    binding.bind.assert_not_called()
    binding.wait_for_device.assert_not_called()
    assert state.bound
