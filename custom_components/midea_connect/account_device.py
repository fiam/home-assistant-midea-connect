"""Explicit LAN credential acquisition using a saved setup account."""
from __future__ import annotations

import logging

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from msmart.const import DeviceType
from msmart.discover import Discover
from msmart.lan import AuthenticationError, ProtocolError, Security

from .const import CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME, PROVIDER_SMARTHOME
from .nethome_account import AccountError, NetHomeAccountClient
from .smarthome_account import SmartHomeAccountClient


class DeviceSetupError(Exception):
    """A user-facing setup failure with a translation key, never cloud text."""


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        # msmart 2026.9.0 logs the LAN session key at INFO and packets at DEBUG.
        return record.levelno >= logging.WARNING


_TRANSPORT_FILTER = _TransportLogFilter()


def protect_transport_logs():
    """Keep transport keys and handshake packets out of Home Assistant logs."""
    logging.getLogger("msmart.lan").addFilter(_TRANSPORT_FILTER)


async def acquire_credentials(http_client, account, host, *, expected_serial=None):
    """Discover, fetch credentials and verify them locally without control writes."""
    provider = account.get(CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME)
    if provider not in (PROVIDER_NETHOME, PROVIDER_SMARTHOME):
        raise DeviceSetupError("account_login_failed")
    protect_transport_logs()
    device = await Discover.discover_single(host, auto_connect=False, timeout=3)
    if device is None:
        raise DeviceSetupError("device_not_found")
    if expected_serial is not None and device.sn != expected_serial:
        raise DeviceSetupError("ble_identity_mismatch")
    if device.type not in (DeviceType.AIR_CONDITIONER, DeviceType.COMMERCIAL_AC):
        raise DeviceSetupError("unsupported_device")
    if device.version != 3:
        raise DeviceSetupError("account_requires_v3")
    cloud = SmartHomeAccountClient(
        http_client) if provider == PROVIDER_SMARTHOME else NetHomeAccountClient(http_client)
    try:
        try:
            await cloud.check_login(account[CONF_EMAIL], account[CONF_PASSWORD])
        except AccountError:
            raise DeviceSetupError("account_login_failed") from None
        cloud_id = None
        if provider == PROVIDER_SMARTHOME:
            try:
                cloud_id = await cloud.appliance_id(device)
            except AccountError as exc:
                raise DeviceSetupError("smarthome_device_not_owned" if exc.code == 3201
                                       else "account_token_unavailable") from None
        failure = "account_token_unavailable"
        # Some modules advertise the big-endian UDPID, others little-endian.
        # A cloud error for one order must not prevent trying the other.
        for byte_order in ("big", "little"):
            udpid = Security.udpid(device.id.to_bytes(6, byte_order)).hex()
            try:
                token, key = (await cloud.get_token(udpid, cloud_id) if cloud_id is not None
                              else await cloud.get_token(udpid))
            except AccountError:
                continue
            try:
                await device.authenticate(token, key)
            except AuthenticationError:
                failure = "account_lan_auth_failed"
                continue
            await device.refresh()
            if not device.online:
                raise DeviceSetupError("cannot_connect")
            if not device.supported:
                raise DeviceSetupError("unsupported_device")
            return device
        raise DeviceSetupError(failure)
    except (OSError, ProtocolError):
        raise DeviceSetupError("cannot_connect") from None
    finally:
        if provider == PROVIDER_SMARTHOME:
            cloud.close()
        # No public close method exists in the pinned msmart version.
        device._lan._disconnect()
