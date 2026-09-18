"""HA progress screens, cancellation and automatic credential storage."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.midea_connect.bluetooth_provision import WifiCredentials
from custom_components.midea_connect.bluetooth_transport import \
    BluetoothSetupError
from custom_components.midea_connect.const import (CONF_ENTRY_KIND, DOMAIN,
                                                   ENTRY_KIND_ACCOUNT)
from custom_components.midea_connect.wifi_credentials import \
    async_get_wifi_store

from .test_bluetooth_discovery import advertisement
from .test_bluetooth_transport import SERIAL


@pytest.fixture(autouse=True)
def no_existing_lan_device():
    with patch("custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan", return_value=None):
        yield


@pytest.fixture
def setup_account(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT,
                                                 "email": "person@example.com", "password": "Account123"})
    entry.add_to_hass(hass)
    return entry


async def complete_progress(hass, result):
    handler = hass.config_entries.flow._progress[result["flow_id"]]
    await handler._ble_task
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    if result["type"] is FlowResultType.SHOW_PROGRESS_DONE:
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def connect(hass, provider="nethome"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=advertisement())
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await complete_progress(hass, result)
    if result.get("step_id") == "bluetooth_lan_choice":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "bluetooth_cloud"})
    if result.get("step_id") == "setup_provider":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"account_provider": provider})
    return result


async def test_probe_then_form_does_not_send_wifi(hass, setup_account):
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)) as probe, \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device") as provision:
        result = await connect(hass)
    assert result["step_id"] == "bluetooth_setup"
    assert result["type"] is FlowResultType.FORM
    probe.assert_awaited_once()
    provision.assert_not_called()


async def test_form_combines_scan_and_saved_networks_and_allows_manual_entry(hass, setup_account):
    await async_get_wifi_store(hass).async_save(WifiCredentials("Home", "Private123"))
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.async_scan_networks", return_value=["Guest", "Home"]):
        result = await connect(hass)
    schema = result["data_schema"]
    assert schema.schema["ssid"].config["options"] == ["Home", "Guest"]
    # A hidden network or an Ethernet-only host must not prevent manual entry.
    assert schema({"account_entry_id": setup_account.entry_id, "ssid": "Hidden"})[
        "ssid"] == "Hidden"
    assert "Private123" not in repr(result)


async def test_existing_wifi_ac_uses_lan_after_failed_bluetooth_without_ip_input(hass, setup_account):
    with patch("custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan",
               return_value=SimpleNamespace(ip="192.0.2.10", sn=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.probe_device", side_effect=BluetoothSetupError("ble_cannot_connect")) as probe:
        result = await connect(hass)
    assert result["step_id"] == "bluetooth_lan"
    assert "host" not in result["data_schema"].schema
    assert result["description_placeholders"]["host"] == "192.0.2.10"
    probe.assert_awaited_once()


async def test_ac_joining_wifi_during_bluetooth_failure_uses_lan(hass, setup_account):
    with patch("custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan",
               return_value=SimpleNamespace(ip="192.0.2.10", sn=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.probe_device", side_effect=BluetoothSetupError("ble_cannot_connect")):
        result = await connect(hass)
    assert result["step_id"] == "bluetooth_lan"
    assert not result["errors"]


async def test_existing_lan_credentials_use_matched_identity(hass, setup_account, create_mock_device):
    device = create_mock_device(1234, "192.0.2.10")
    device.port = 6444
    device.token, device.key = "ab" * 64, "cd" * 32
    with patch("custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan",
               return_value=SimpleNamespace(ip=device.ip, sn=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.probe_device", side_effect=BluetoothSetupError("ble_cannot_connect")), \
            patch("custom_components.midea_connect.bluetooth_flow.acquire_credentials", return_value=device) as acquire, \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device") as provision, \
            patch("custom_components.midea_connect.async_setup_entry", return_value=True):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id})
        result = await complete_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert acquire.call_args.kwargs["expected_serial"] == SERIAL
    provision.assert_not_called()


async def test_probe_failure_offers_retry_without_wifi_form(hass, setup_account):
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", side_effect=BluetoothSetupError("ble_cannot_connect")):
        result = await connect(hass)
    assert result["step_id"] == "bluetooth_connect_retry"
    assert result["errors"] == {"base": "ble_cannot_connect"}


async def test_success_saves_wifi_separately_from_device_keys(hass, setup_account, create_mock_device):
    device = create_mock_device(1234, "192.0.2.10")
    device.port = 6444
    device.token, device.key = "ab" * 64, "cd" * 32

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        assert wifi.password == "WifiPrivate123"
        state.write_started = state.bound = True
        state.serial = SERIAL
        state.appliance_code = "1234"
        return device
    with (
        patch("custom_components.midea_connect.bluetooth_flow.probe_device",
              return_value=SimpleNamespace(serial=SERIAL)),
        patch("custom_components.midea_connect.bluetooth_flow.provision_device",
              side_effect=provision),
        patch("custom_components.midea_connect.async_setup_entry", return_value=True),
    ):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home", "wifi_password": "WifiPrivate123"})
        handler = hass.config_entries.flow._progress[result["flow_id"]]
        result = await complete_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["token"] == device.token
    assert result["data"]["k1"] == device.key
    assert result["data"]["bluetooth_address"] == advertisement().address
    assert result["data"]["sn"] == SERIAL
    assert "WifiPrivate123" not in repr(result) + repr(vars(handler))
    assert handler._ble_wifi is None
    assert handler._ble_wifi_to_save is None
    saved = await async_get_wifi_store(hass).async_get("Home")
    assert saved.password == "WifiPrivate123"


async def test_cancel_provision_cancels_task_and_clears_password(hass, setup_account):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def provision(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device", side_effect=provision):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home", "wifi_password": "WifiPrivate123"})
        handler = hass.config_entries.flow._progress[result["flow_id"]]
        await started.wait()
        wifi = handler._ble_wifi
        hass.config_entries.flow.async_abort(result["flow_id"])
        await cancelled.wait()
    assert not wifi.password
    assert handler._ble_task.cancelled()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert handler._ble_wifi_to_save is None
    assert await async_get_wifi_store(hass).async_networks() == []


async def test_invalid_wifi_input_is_not_echoed(hass, setup_account):
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device") as provision:
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home", "wifi_password": "short"})
    assert result["errors"] == {"base": "ble_invalid_wifi_password"}
    assert "short" not in repr(result)
    provision.assert_not_called()


@pytest.mark.parametrize("replacement,use_saved,expected", [
    ("", True, "Existing123"), ("Replacement123", True, "Replacement123"),
    ("", False, ""),
])
async def test_saved_network_reuse_and_password_override(
        hass, setup_account, replacement, use_saved, expected, caplog):
    store = async_get_wifi_store(hass)
    await store.async_save(WifiCredentials("Home", "Existing123"))

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        assert wifi.ssid == "Home" and wifi.password == expected
        # A rejected password must never replace the saved, working one.
        raise BluetoothSetupError("ble_wifi_wrong_password")

    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device", side_effect=provision):
        result = await connect(hass)
        schema = result["data_schema"].schema
        assert schema["ssid"].config["options"] == ["Home"]
        assert "Existing123" not in repr(result)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home",
            "wifi_password": replacement,
            "password_mode": "open" if expected == "" else "new" if replacement else "saved"})
        handler = hass.config_entries.flow._progress[result["flow_id"]]
        result = await complete_progress(hass, result)
    assert result["errors"]["base"] == "ble_wifi_wrong_password"
    assert (await store.async_get("Home")).password == "Existing123"
    assert handler._ble_wifi_to_save is None
    # HA's test-only storage mock logs its fake contents at DEBUG. Production
    # storage does not; inspect application/HA logs without that fixture logger.
    application_logs = " ".join(r.getMessage() for r in caplog.records
                                if not r.name.startswith("pytest_homeassistant"))
    assert "Existing123" not in repr(result) + application_logs
    assert "Replacement123" not in repr(result) + application_logs


@pytest.mark.parametrize('saved_ssid,use_saved', [(None, True), ('Different network', True), ('Home', False)])
async def test_blank_wifi_password_without_usable_saved_profile_never_provisions(
        hass, setup_account, saved_ssid, use_saved):
    if saved_ssid:
        await async_get_wifi_store(hass).async_save(WifiCredentials(saved_ssid, 'SavedPassword'))
    with patch('custom_components.midea_connect.bluetooth_flow.probe_device', return_value=SimpleNamespace(serial=SERIAL)), \
            patch('custom_components.midea_connect.bluetooth_flow.provision_device') as provision:
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {
            'account_entry_id': setup_account.entry_id, 'ssid': 'Home',
            'wifi_password': '', 'password_mode': 'saved' if saved_ssid and use_saved else 'new'})
    assert result['step_id'] == 'bluetooth_setup'
    assert result['errors'] == {'base': 'ble_wifi_password_required'}
    provision.assert_not_called()


async def test_open_network_with_entered_password_requires_correction(hass, setup_account):
    with patch('custom_components.midea_connect.bluetooth_flow.probe_device', return_value=SimpleNamespace(serial=SERIAL)), \
            patch('custom_components.midea_connect.bluetooth_flow.provision_device') as provision:
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {
            'account_entry_id': setup_account.entry_id, 'ssid': 'Home',
            'wifi_password': 'Private123', 'password_mode': 'open'})
    assert result['errors'] == {'base': 'ble_open_network_password'}
    assert 'Private123' not in repr(result)
    provision.assert_not_called()


async def test_password_survives_uncertain_write_until_successful_retry(
        hass, setup_account, create_mock_device):
    device = create_mock_device(1234, "192.0.2.10")
    device.port, device.token, device.key = 6444, "ab" * 64, "cd" * 32
    calls = 0

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        nonlocal calls
        calls += 1
        if calls == 1:
            state.write_started = True
            wifi.clear()
            raise BluetoothSetupError("ble_setup_incomplete")
        assert wifi is None  # Retry must not resend the network password.
        state.bound = True
        return device

    store = async_get_wifi_store(hass)
    await store.async_save(WifiCredentials("Home", "Existing123"))
    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device", side_effect=provision), \
            patch("custom_components.midea_connect.async_setup_entry", return_value=True):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home", "wifi_password": "Replacement123"})
        result = await complete_progress(hass, result)
        assert result["step_id"] == "bluetooth_finish"
        assert (await store.async_get("Home")).password == "Existing123"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await complete_progress(hass, result)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (await store.async_get("Home")).password == "Replacement123"


async def test_binding_rejection_code_renders_and_clears_on_retry(hass, setup_account):
    calls = 0

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        nonlocal calls
        calls += 1
        if calls == 1:
            state.write_started = True
            wifi.clear()
            raise BluetoothSetupError("ble_binding_rejected", cloud_code=123)
        assert wifi is None
        raise BluetoothSetupError("ble_binding_failed")

    with patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)), \
            patch("custom_components.midea_connect.bluetooth_flow.provision_device", side_effect=provision):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "account_entry_id": setup_account.entry_id, "ssid": "Home", "wifi_password": "Private123"})
        result = await complete_progress(hass, result)
        assert result["step_id"] == "bluetooth_finish"
        assert result["errors"]["base"] == "ble_binding_rejected"
        assert result["description_placeholders"] == {
            "name": "AC 7E4D", "code": "123"}
        assert "Private123" not in repr(result)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await complete_progress(hass, result)
    assert result["errors"]["base"] == "ble_binding_failed"
    assert result["description_placeholders"] == {"name": "AC 7E4D"}


async def test_lan_repair_keeps_smarthome_selection_and_names_both_providers(hass, setup_account):
    smart = MockConfigEntry(domain=DOMAIN, data={
        **setup_account.data, 'account_provider': 'smarthome'})
    smart.add_to_hass(hass)
    with patch('custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan',
               return_value=SimpleNamespace(ip='192.0.2.10', sn=SERIAL)), \
            patch('custom_components.midea_connect.bluetooth_flow.probe_device',
                  side_effect=[BluetoothSetupError("ble_cannot_connect"), SimpleNamespace(serial=SERIAL)]) as probe:
        result = await connect(hass, "smarthome")
        options = result['data_schema'].schema['account_entry_id'].container
        assert options[smart.entry_id].startswith('SmartHome:')
        assert setup_account.entry_id not in options
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {
            'account_entry_id': smart.entry_id, 'setup_wifi': True})
        result = await complete_progress(hass, result)
    assert result['step_id'] == 'bluetooth_setup'
    assert probe.await_count == 2
    marker = next(
        k for k in result['data_schema'].schema if k == 'account_entry_id')
    assert marker.default() == smart.entry_id


async def test_confirmation_form_preserves_progress_and_explicit_restart(hass, setup_account):
    calls = []

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        calls.append((state.confirmation_started, wifi is None))
        state.write_started = True
        state.appliance_code = '998877'
        state.confirmation_started = True
        state.confirmation_instructions = 'Press the controller pairing button.'
        raise BluetoothSetupError('smarthome_confirmation_required')

    with patch('custom_components.midea_connect.bluetooth_flow.probe_device', return_value=SimpleNamespace(serial=SERIAL)), \
            patch('custom_components.midea_connect.bluetooth_flow.provision_device', side_effect=provision):
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {
            'account_entry_id': setup_account.entry_id, 'ssid': 'Home', 'wifi_password': 'Private123'})
        result = await complete_progress(hass, result)
        assert result['step_id'] == 'bluetooth_confirm'
        assert result['description_placeholders'] == {
            'name': 'AC 7E4D', 'instructions': 'Press the controller pairing button.'}
        assert 'Private123' not in repr(result)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {})
        result = await complete_progress(hass, result)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'restart_confirmation': True})
        result = await complete_progress(hass, result)
    assert result['step_id'] == 'bluetooth_confirm'
    assert calls == [(False, False), (True, True), (False, True)]


async def test_expired_pairing_reviews_wifi_before_any_new_write(hass, setup_account):
    async def provision(_hass, _http, _discovery, _account, state, wifi):
        state.write_started = state.binding_expired = True
        raise BluetoothSetupError('smarthome_pairing_expired', cloud_code=1383)

    with patch('custom_components.midea_connect.bluetooth_flow.probe_device', return_value=SimpleNamespace(serial=SERIAL)), \
            patch('custom_components.midea_connect.bluetooth_flow.provision_device', side_effect=provision) as write:
        result = await connect(hass)
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {
            'account_entry_id': setup_account.entry_id, 'ssid': 'Home', 'wifi_password': 'Private123'})
        result = await complete_progress(hass, result)
        assert result['step_id'] == 'bluetooth_pairing_expired'
        assert result['description_placeholders'] == {'name': 'AC 7E4D'}
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {})
        if result['type'] is FlowResultType.SHOW_PROGRESS:
            result = await complete_progress(hass, result)
    assert result['step_id'] == 'bluetooth_setup'
    assert 'Private123' not in repr(result)
    write.assert_awaited_once()
    handler = hass.config_entries.flow._progress[result['flow_id']]
    assert not handler._ble_state.write_started
    assert not handler._ble_state.binding_expired
    assert handler._setup_account_entry_id == setup_account.entry_id


async def test_stage_updates_do_not_refresh_a_completed_flow(hass, setup_account, create_mock_device):
    """A fast final stage must not queue a stale GET after entry creation."""
    from homeassistant.data_entry_flow import (
        EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE,
        EVENT_DATA_ENTRY_FLOW_PROGRESSED)
    refreshes, updates = [], []
    device = create_mock_device(1234, "192.0.2.10")
    device.port, device.token, device.key = 6444, "ab" * 64, "cd" * 32
    release = asyncio.Event()

    async def provision(_hass, _http, _discovery, _account, state, wifi):
        await release.wait()
        for stage in ("bluetooth_connected", "cloud_lookup_started",
                      "lan_search_started", "lan_found", "local_credentials_verified"):
            state.trace.record(stage)
        state.write_started = state.bound = True
        return device

    with (patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)),
          patch("custom_components.midea_connect.bluetooth_flow.provision_device",
                side_effect=provision),
          patch("custom_components.midea_connect.async_setup_entry", return_value=True)):
        result = await connect(hass)
        await hass.async_block_till_done()
        unsubscribe_refresh = hass.bus.async_listen(
            EVENT_DATA_ENTRY_FLOW_PROGRESSED, lambda event: refreshes.append(event.data))
        unsubscribe_update = hass.bus.async_listen(
            EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE, lambda event: updates.append(event.data))
        try:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {
                "account_entry_id": setup_account.entry_id, "ssid": "Home",
                "password_mode": "new", "wifi_password": "Private123"})
            assert result["progress_action"] == "bluetooth_provisioning"
            release.set()
            await hass.async_block_till_done()
            # The manager requests one completion refresh; stage updates only
            # emit numerical progress and cannot fetch an already removed flow.
            assert len(refreshes) == 1
            assert [update["progress"]
                    for update in updates] == [0.2, 0.45, 0.65, 0.8, 0.95]
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert len(hass.config_entries.async_entries(DOMAIN)) == 2
        finally:
            unsubscribe_refresh()
            unsubscribe_update()


async def test_nearby_connect_probes_bluetooth_before_any_lan_scan(hass, setup_account):
    with (patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)) as probe,
          patch("custom_components.midea_connect.bluetooth_flow.find_bluetooth_device_on_lan") as lan):
        result = await connect(hass)
    assert result['step_id'] == 'bluetooth_setup'
    probe.assert_awaited_once()
    lan.assert_not_called()


async def test_discovery_waits_for_add_then_connects_without_confirmation(hass, setup_account):
    with (patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial=SERIAL)) as probe,
          patch("custom_components.midea_connect.bluetooth_flow.provision_device") as provision):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "bluetooth"}, data=advertisement())
        await hass.async_block_till_done()
        probe.assert_not_called()
        assert result["step_id"] == "bluetooth_found"
        # HA's Add button opens/advances the flow without submitting a form.
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        result = await complete_progress(hass, result)
        assert result["step_id"] == "setup_provider"
        probe.assert_awaited_once()
        provision.assert_not_called()
