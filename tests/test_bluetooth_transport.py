"""Exercise the actual encrypted exchange against an independent fake peripheral."""
import asyncio
from hashlib import md5
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.midea_connect.bluetooth_discovery import \
    MideaBluetoothDiscovery
from custom_components.midea_connect.bluetooth_transport import (
    BOOTSTRAP, PROOF, BluetoothSetupError, FrameBuffer, MideaBluetoothSession,
    decrypt, encode_frame, encrypt, probe_device, validate_wifi, wifi_payload)

SERIAL = "000000P0000000Q1B21A2B3C7E4D0000"
DISCOVERY = MideaBluetoothDiscovery(
    "B2:1A:2B:3C:7E:4E", "00000Q1B", "7E4D", 1, -77)


class Peripheral:
    def __init__(self):
        self.is_connected = True
        self.disconnected = False
        self.buffer = FrameBuffer()
        self.private = ec.derive_private_key(7, ec.SECP256R1())
        self.commands = []
        self.key = None
        self.callback = None
        self.reject_handshake = False
        self.wifi_ack = 0

    async def start_notify(self, _uuid, callback):
        self.callback = callback

    def reply(self, sequence, command, body, key):
        packet = encode_frame(sequence, command, body, key)
        # Notifications may be fragmented differently from write chunks.
        for offset in range(0, len(packet), 7):
            self.callback(None, packet[offset:offset + 7])

    async def write_gatt_char(self, _uuid, chunk, response):
        assert response is True and len(chunk) <= 20
        for frame in self.buffer.feed(chunk):
            sequence, command = frame[3:5]
            body = decrypt(
                frame[5:-1], BOOTSTRAP if command == 1 else self.key)
            self.commands.append((command, body))
            if command == 1 and body[0] == 1:
                public = self.private.public_key().public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]
                self.reply(sequence, command, b"\x01\x02" + public, BOOTSTRAP)
            elif command == 1:
                peer = ec.EllipticCurvePublicKey.from_encoded_point(
                    ec.SECP256R1(), b"\x04" + body[2:66])
                self.key = self.private.exchange(ec.ECDH(), peer)[:16]
                assert decrypt(body[66:], self.key) == PROOF
                self.reply(sequence, command, bytes(
                    [2, 2, int(self.reject_handshake)]), BOOTSTRAP)
            elif command == 0x63:
                assert body == bytes(19)
                response_body = b"\0" + SERIAL.encode() + bytes(6) + b"\x06" + \
                    bytes.fromhex("150029092122")
                self.reply(sequence, command, response_body, self.key)
            elif command == 0x69:
                # An asynchronous status may precede the command acknowledgement.
                self.reply(sequence, 0x0D, bytes([1, 4, 0]), self.key)
                self.reply(sequence, command, bytes([self.wifi_ack]), self.key)
            else:
                pytest.fail("Unexpected device command")

    async def disconnect(self):
        self.is_connected = False
        self.disconnected = True


@pytest.fixture
def peripheral():
    peer = Peripheral()
    with patch("custom_components.midea_connect.bluetooth_transport.establish_connection", return_value=peer), \
            patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_scanner_devices_by_address", return_value=[object()]), \
            patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_ble_device_from_address", return_value=object()):
        yield peer


async def test_probe_handshake_queries_identity_without_wifi_writes(hass, peripheral, caplog):
    info = await probe_device(hass, DISCOVERY)
    assert info.serial == SERIAL
    assert info.version == "150029092122"
    assert [command for command, _ in peripheral.commands] == [1, 1, 0x63]
    assert peripheral.disconnected
    assert peripheral.key.hex() not in caplog.text


async def test_client_resolved_after_ha_installs_proxy_dispatcher(hass, peripheral):
    with patch("custom_components.midea_connect.bluetooth_transport.bleak.BleakClient") as current_client, \
            patch("custom_components.midea_connect.bluetooth_transport.establish_connection", return_value=peripheral) as connect:
        await probe_device(hass, DISCOVERY)
    assert connect.call_args.args[0] is current_client


async def test_cached_discovery_refreshes_shared_proxy_before_connecting(hass, peripheral):
    with patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_scanner_devices_by_address",
               side_effect=[[], [object()]]), \
            patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_request_active_scan") as sweep:
        info = await probe_device(hass, DISCOVERY)
    sweep.assert_awaited_once_with(hass, 8)
    assert info.serial == SERIAL


async def test_stale_discovery_without_live_path_does_not_attempt_connection(hass, peripheral):
    with patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_scanner_devices_by_address", return_value=[]), \
            patch("custom_components.midea_connect.bluetooth_transport.bluetooth.async_request_active_scan"), \
            patch("custom_components.midea_connect.bluetooth_transport.establish_connection") as connect:
        with pytest.raises(BluetoothSetupError, match="ble_not_connectable"):
            await probe_device(hass, DISCOVERY)
    connect.assert_not_called()


@pytest.mark.parametrize("ack", [0, 3])
async def test_encrypted_wifi_ack_and_early_status(hass, peripheral, caplog, ack):
    started = []
    statuses = []
    peripheral.wifi_ack = ack
    async with MideaBluetoothSession(hass, DISCOVERY) as session:
        await session.device_info()
        payload, random_code = wifi_payload(
            "Café", "Private123", DISCOVERY.address)
        assert await session.configure_wifi(payload, lambda: started.append(True)) == ack
        await session.wait_for_network(on_status=lambda status, error: statuses.append((status, error)))
        sent = peripheral.commands[-1]
        assert sent == (0x69, payload)
        assert len(random_code) == 16
    assert session._key is None
    assert peripheral.disconnected
    assert started == [True]
    assert statuses == [(4, 0)]
    assert "Private123" not in caplog.text


async def test_rejected_session_proof_disconnects(hass, peripheral):
    peripheral.reject_handshake = True
    with pytest.raises(BluetoothSetupError, match="ble_handshake_failed"):
        await probe_device(hass, DISCOVERY)
    assert peripheral.disconnected
    assert [c for c, _ in peripheral.commands] == [1, 1]


async def test_cancellation_disconnects_session(hass, peripheral):
    entered = asyncio.Event()

    async def work():
        async with MideaBluetoothSession(hass, DISCOVERY):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peripheral.disconnected


async def test_bad_notification_fails_status_wait_immediately(hass, peripheral):
    async with MideaBluetoothSession(hass, DISCOVERY) as session:
        session._receive(None, b"bad")
        with pytest.raises(BluetoothSetupError, match="ble_invalid_response"):
            await session.wait_for_network()
        assert not session._buffer.buffer


def test_wifi_wire_layout_uses_utf8_lengths_and_app_random_code():
    with patch("custom_components.midea_connect.bluetooth_transport.token_bytes", return_value=b"\x12\x34"):
        payload, code = wifi_payload("Café", "Private123", DISCOVERY.address)
    mac = bytes.fromhex("b21a2b3c7e4e")
    assert payload[:10] == b"\x02" + mac + bytes([0, 5, 10])
    assert payload[10:25] == "Café".encode() + b"Private123"
    assert code == b"\x12\x34" + md5(mac + b"Private123").digest()[:14]
    assert payload[25:] == code + bytes(5)


@pytest.mark.parametrize("ssid,password", [("", "Private123"), ("é" * 17, "Private123"),
                                           ("Home", "short"), ("Home", "x" * 64), ("Home\0bad", "Private123")])
def test_invalid_wifi_never_encodes(ssid, password):
    with pytest.raises(BluetoothSetupError):
        wifi_payload(ssid, password, DISCOVERY.address)


@pytest.mark.parametrize("password", ["", "x" * 63, "a" * 64])
def test_supported_password_formats(password):
    validate_wifi("Home", password)


def test_frames_reject_corruption_and_overflow():
    packet = bytearray(encode_frame(1, 0x63, bytes(19), BOOTSTRAP))
    packet[-1] ^= 1
    with pytest.raises(BluetoothSetupError):
        FrameBuffer().feed(packet)
    with pytest.raises(BluetoothSetupError):
        FrameBuffer().feed(bytes(2049))


async def test_identity_mismatch_stops_before_wifi(hass, peripheral):
    wrong = MideaBluetoothDiscovery(
        DISCOVERY.address, DISCOVERY.sn8, "FFFF", 1, -77)
    with pytest.raises(BluetoothSetupError, match="ble_identity_mismatch"):
        await probe_device(hass, wrong)
    assert peripheral.disconnected
    assert all(c != 0x69 for c, _ in peripheral.commands)
