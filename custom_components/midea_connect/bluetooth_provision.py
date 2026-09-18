"""Connect an identified AC to Wi-Fi, bind it, and verify its LAN credentials."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from secrets import token_hex
from time import monotonic

from bleak import BleakError
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from msmart.const import DeviceType
from msmart.discover import Discover

from .account_device import DeviceSetupError, acquire_credentials
from .bluetooth_transport import (BluetoothSetupError, MideaBluetoothSession,
                                  wifi_payload)
from .const import CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME, PROVIDER_SMARTHOME
from .nethome_account import AccountError, NetHomeAccountClient
from .nethome_binding import NetHomeBindingClient
from .smarthome_account import SmartHomeAccountClient
from .smarthome_binding import SmartHomeBindingClient

_LOGGER = logging.getLogger(__name__)


@dataclass(repr=False)
class SetupTrace:
    """Bounded timings and numeric protocol codes, never device/account data."""

    attempt: str = field(default_factory=lambda: token_hex(4))
    started: float | None = None
    wifi_started: float | None = None
    events: list = field(default_factory=list)
    on_stage: object = field(default=None, repr=False)

    def record(self, stage, *, code=None, status=None):
        if self.on_stage is not None:
            self.on_stage(stage)
        now = monotonic()
        if self.started is None:
            self.started = now
        if stage == "wifi_write_started":
            self.wifi_started = now
        event = {"stage": stage, "elapsed_ms": round(
            (now - self.started) * 1000)}
        if self.wifi_started is not None:
            event["since_wifi_write_ms"] = round(
                (now - self.wifi_started) * 1000)
        if type(code) is int:
            event["code"] = code
        if type(status) is int:
            event["status"] = status
        self.events.append(event)
        del self.events[:-48]

    def as_dict(self):
        return {"attempt": self.attempt, "events": self.events}


@dataclass(repr=False)
class WifiCredentials:
    ssid: str
    password: str

    def clear(self):
        self.ssid = self.password = ""


@dataclass
class ProvisioningState:
    """Transient progress survives Retry without resending Wi-Fi credentials."""

    serial: str = ""
    write_started: bool = False
    wifi_acknowledged: bool = False
    bound: bool = False
    host: str = ""
    appliance_code: str = ""
    confirmation_started: bool = False
    confirmation_instructions: str = ""
    binding_expired: bool = False
    random_code: bytes = field(default=b"", repr=False)
    trace: SetupTrace = field(default_factory=SetupTrace, repr=False)

    def clear(self):
        self.random_code = b""


async def find_bluetooth_device_on_lan(discovery):
    """Match an advertisement to a single AC, never just a friendly name."""
    devices = await Discover.discover(auto_connect=False, timeout=2)
    matches = [device for device in devices
               if device.type in (DeviceType.AIR_CONDITIONER, DeviceType.COMMERCIAL_AC)
               and isinstance(device.sn, str) and re.fullmatch(r"[A-Za-z0-9]{32}", device.sn)
               and discovery.sn8 in device.sn
               and device.sn[-8:-4].upper() == discovery.suffix]
    return matches[0] if len(matches) == 1 else None


async def find_on_lan(serial, timeout=60):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        devices = await Discover.discover(auto_connect=False, timeout=3)
        for device in devices:
            if device.sn == serial:
                return device
        await asyncio.sleep(2)
    raise BluetoothSetupError("ble_lan_not_found")


async def provision_device(hass, http_client, discovery, account, state, wifi=None):
    """Run only incomplete setup stages and return a locally verified AC."""
    provider = account.get(CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME)
    if provider not in (PROVIDER_NETHOME, PROVIDER_SMARTHOME):
        if wifi is not None:
            wifi.clear()
        raise BluetoothSetupError("account_login_failed")
    smart = provider == PROVIDER_SMARTHOME
    cloud = SmartHomeAccountClient(
        http_client) if smart else NetHomeAccountClient(http_client)
    binding = None
    completed = False
    state.trace.record("resume" if state.write_started else "start")
    try:
        async with asyncio.timeout(240):
            try:
                await cloud.check_login(account[CONF_EMAIL], account[CONF_PASSWORD])
                state.trace.record("account_login_ok")
                binding = (SmartHomeBindingClient(cloud, hass.config.time_zone) if smart
                           else NetHomeBindingClient(http_client, cloud))
                if not state.bound:
                    await binding.prepare()
                    if smart and not state.write_started:
                        # Verify the native authenticated transport before Wi-Fi
                        # is written. A registered AC may still need new Wi-Fi.
                        await binding.owned_id(state.serial)
                        state.trace.record("native_preflight_ok")
            except AccountError:
                raise BluetoothSetupError("account_login_failed") from None
            if not state.write_started:
                if wifi is None:
                    raise BluetoothSetupError("ble_invalid_parameters")
                async with MideaBluetoothSession(hass, discovery) as session:
                    state.trace.record("bluetooth_connected")
                    info = await session.device_info()
                    if state.serial and info.serial != state.serial:
                        raise BluetoothSetupError("ble_identity_mismatch")
                    state.serial = info.serial
                    payload, state.random_code = wifi_payload(
                        wifi.ssid, wifi.password, discovery.address, mode=1 if smart else 2)
                    wifi.clear()

                    def write_started():
                        state.write_started = True
                        state.trace.record("wifi_write_started")
                    try:
                        ack = await session.configure_wifi(payload, write_started)
                        state.wifi_acknowledged = True
                        state.trace.record("wifi_ack", code=ack)
                    except BluetoothSetupError as exc:
                        if str(exc) in {"ble_invalid_parameters", "ble_auth_required", "ble_unsupported_wifi",
                                        "ble_already_bound", "ble_wifi_rejected"}:
                            # Explicit rejection, not a lost ACK.
                            state.write_started = False
                        raise
                    finally:
                        payload = b""
                    if smart:
                        # The app starts cloud discovery immediately after the
                        # Wi-Fi ACK. Waiting for BLE status first can consume
                        # the server's short binding window (error 1383).
                        await _link_smarthome(binding, discovery, state, session=session)
                    else:
                        await _wait_for_network(session, state)
            if not state.bound and smart:
                await _link_smarthome(binding, discovery, state)
            elif not state.bound:
                try:
                    state.trace.record("cloud_lookup_started")
                    code, verification = await binding.wait_for_device(state.serial, state.random_code)
                except AccountError:
                    raise BluetoothSetupError(
                        "ble_cloud_device_not_found") from None
                try:
                    await binding.bind(code, verification, discovery.name,
                                       appliance_type=discovery.device_type)
                except AccountError as exc:
                    reason = "ble_binding_rejected" if exc.code is not None else "ble_binding_failed"
                    raise BluetoothSetupError(
                        reason, cloud_code=exc.code) from None
                state.appliance_code = code
                state.bound = True
                state.clear()
            state.trace.record("lan_search_started")
            device = await find_on_lan(state.serial)
            state.host = device.ip
            state.trace.record("lan_found")
            device = await acquire_credentials(http_client, account, device.ip, expected_serial=state.serial)
            state.trace.record("local_credentials_verified")
            completed = True
            return device
    except (BleakError, OSError, TimeoutError):
        reason = "ble_setup_incomplete" if state.write_started else "ble_cannot_connect"
        raise BluetoothSetupError(reason) from None
    finally:
        if smart:
            state.trace.record("complete" if completed else "incomplete")
            # Numeric results and fixed stage labels only. Never serialize the
            # state, exception, serial, Wi-Fi settings, proof or cloud response.
            _LOGGER.log(logging.INFO if completed else logging.WARNING,
                        "SmartHome setup trace: %s", json.dumps(state.trace.as_dict()))
        if wifi is not None:
            wifi.clear()
        if binding is not None:
            binding.close()
        elif smart:
            cloud.close()


async def _wait_for_network(session, state):
    last_status = None

    def status_received(status, error):
        nonlocal last_status
        if (status, error) != last_status:
            state.trace.record("ble_network_status", status=status, code=error)
            last_status = (status, error)

    try:
        await session.wait_for_network(on_status=status_received)
        state.trace.record("ble_network_complete")
    except BluetoothSetupError as exc:
        state.trace.record("ble_network_unavailable")
        if str(exc) in ("ble_wifi_wrong_password", "ble_wifi_not_found"):
            state.write_started = False
            raise
        # Firmware may disconnect BLE when it joins Wi-Fi. Cloud lookup and
        # exact-serial LAN discovery decide success if status is unavailable.
    except (BleakError, TimeoutError):
        state.trace.record("ble_network_unavailable")


async def _wait_for_smarthome_device(binding, discovery, state, session):
    """Poll cloud proof alongside BLE status; never delay binding for BLE."""
    lookup = asyncio.create_task(binding.wait_for_device(
        state.serial, state.random_code, discovery.device_type))
    network = asyncio.create_task(_wait_for_network(session, state))
    try:
        await asyncio.wait((lookup, network), return_when=asyncio.FIRST_COMPLETED)
        if network.done():
            # Report explicit wrong-password/SSID errors before attempting a
            # bind. Success or missing BLE status does not prove cloud arrival.
            network.result()
        return await lookup
    finally:
        # Only read operations run in these tasks. No bind/confirmation write
        # is started until both tasks have been joined, including cancellation.
        for task in (lookup, network):
            if not task.done():
                task.cancel()
        await asyncio.gather(lookup, network, return_exceptions=True)


async def _link_smarthome(binding, discovery, state, *, session=None):
    """Resume linking/confirmation without repeating successful mutations."""
    try:
        state.trace.record("ownership_check_started")
        owned = await binding.owned_id(state.serial)
        state.trace.record("ownership_check_done", status=int(bool(owned)))
    except AccountError:
        raise BluetoothSetupError("ble_binding_failed") from None
    if owned and state.appliance_code and owned != state.appliance_code:
        raise BluetoothSetupError("ble_identity_mismatch")
    if not state.appliance_code:
        state.appliance_code = owned or ""
    if not state.appliance_code:
        if state.binding_expired:
            raise BluetoothSetupError(
                "smarthome_pairing_expired", cloud_code=1383)
        try:
            state.trace.record("cloud_lookup_started")
            if session is None:
                metadata = await binding.wait_for_device(
                    state.serial, state.random_code, discovery.device_type)
            else:
                metadata = await _wait_for_smarthome_device(binding, discovery, state, session)
            state.trace.record("cloud_proof_found")
        except AccountError as exc:
            state.trace.record("cloud_lookup_rejected", code=exc.code)
            raise BluetoothSetupError("smarthome_lookup_failed" if exc.code is not None
                                      else "ble_cloud_device_not_found", cloud_code=exc.code) from None
        try:
            state.trace.record("bind_started")
            state.appliance_code = await binding.bind(
                state.serial, discovery.name, discovery.device_type, **metadata)
            state.trace.record("bind_accepted")
        except AccountError as exc:
            state.trace.record("bind_rejected", code=exc.code)
            # A timeout or malformed reply may hide a successful bind. Read the
            # account's exact serial before reporting failure or retrying later.
            try:
                state.appliance_code = await binding.owned_id(state.serial) or ""
            except AccountError:
                pass
            if not state.appliance_code:
                if exc.code == 1383:
                    state.binding_expired = True
                    state.clear()
                    raise BluetoothSetupError(
                        "smarthome_pairing_expired", cloud_code=1383) from None
                raise BluetoothSetupError("ble_binding_rejected" if exc.code is not None
                                          else "ble_binding_failed", cloud_code=exc.code) from None
    if owned and session is not None:
        # Ownership survives a Wi-Fi reset. It does not mean that a fresh
        # CONNECT has finished DHCP/cloud login. Consume its network status
        # before querying confirmation; otherwise the server can return 3123
        # immediately and the flow drops BLE while the AC is still joining.
        await _wait_for_network(session, state)
    try:
        status = await binding.confirmation_status(state.appliance_code)
        state.trace.record("confirmation_status", status=status)
        if status in (1, 2):
            if not state.confirmation_started:
                # Mark before sending: a lost response must not restart the
                # physical confirmation window on an ordinary Retry.
                state.confirmation_started = True
                try:
                    await binding.start_confirmation(state.appliance_code)
                except AccountError as exc:
                    if exc.code is not None:
                        state.confirmation_started = False
                    raise
                status = await binding.confirmation_status(state.appliance_code)
            if status in (1, 2):
                state.confirmation_instructions = await binding.confirmation_instructions(
                    state.serial, discovery.device_type)
                raise BluetoothSetupError("smarthome_confirmation_required")
    except AccountError as exc:
        state.trace.record("confirmation_check_failed", code=exc.code)
        # SmartHome's Android CustomErrorCode maps 3123 to device offline.
        # Keep the appliance ID and completed writes for a read-only retry.
        if exc.code == 3123:
            raise BluetoothSetupError(
                "smarthome_device_offline", cloud_code=exc.code) from None
        raise BluetoothSetupError(
            "smarthome_confirmation_failed", cloud_code=exc.code) from None
    # Do not accept a bind ID alone as ownership. This is also the prerequisite
    # for token retrieval. Eventual consistency is handled by explicit Retry.
    try:
        if await binding.owned_id(state.serial) != state.appliance_code:
            raise BluetoothSetupError("smarthome_ownership_pending")
    except AccountError:
        raise BluetoothSetupError("ble_binding_failed") from None
    state.bound = True
    state.trace.record("ownership_verified")
    state.confirmation_instructions = ""
    state.clear()
