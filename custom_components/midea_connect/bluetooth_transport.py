"""Midea BLE-v1 handshake, information query and encrypted Wi-Fi setup.

Based on NetHome Plus 5.44.0's MSBle0Device/MSDeviceBleConfig1Task and the
successful local handshake probe. No factory-mode, reset or AC control writes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from hashlib import md5
from secrets import token_bytes

import bleak
from bleak import BleakError
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant.components import bluetooth

WRITE_UUID = "0000ff81-0000-1000-8000-00805f9b34fb"
REPLY_UUID = "0000ff82-0000-1000-8000-00805f9b34fb"
BOOTSTRAP = b"xhdiwjnchekd4d51"
PROOF = b"midea_blekeyc"
_LOGGER = logging.getLogger(__name__)


class BluetoothSetupError(Exception):
    """A translation key and optional numeric code; never packets or credentials."""

    def __init__(self, reason, *, cloud_code=None):
        super().__init__(reason)
        self.cloud_code = cloud_code if type(cloud_code) is int else None


@dataclass(frozen=True)
class BluetoothDeviceInfo:
    serial: str
    version: str


def encrypt(body: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(body) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def decrypt(body: bytes, key: bytes) -> bytes:
    if not body or len(body) % 16:
        raise BluetoothSetupError("ble_invalid_response")
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = cipher.update(body) + cipher.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise BluetoothSetupError("ble_invalid_response") from None


def encode_frame(sequence: int, command: int, body: bytes, key: bytes) -> bytes:
    encrypted = encrypt(body, key)
    if len(encrypted) + 4 > 255 or not 1 <= sequence <= 255:
        raise BluetoothSetupError("ble_invalid_parameters")
    content = bytes([len(encrypted) + 4, sequence, command]) + encrypted
    return b"\xaa\x55" + content + bytes([-sum(content) & 255])


class FrameBuffer:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        if len(self.buffer) > 2048:
            raise BluetoothSetupError("ble_invalid_response")
        frames = []
        while len(self.buffer) >= 3:
            length = self.buffer[2] + 2
            if self.buffer[:2] != b"\xaa\x55" or length < 6:
                raise BluetoothSetupError("ble_invalid_response")
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            del self.buffer[:length]
            if sum(frame[2:]) & 255:
                raise BluetoothSetupError("ble_invalid_response")
            frames.append(frame)
        return frames


def parse_device_info(body: bytes) -> BluetoothDeviceInfo:
    if len(body) < 40 or body[0] != 0 or len(body) < 40 + body[39]:
        raise BluetoothSetupError("ble_invalid_response")
    if not re.fullmatch(rb"[A-Za-z0-9]{32}", body[1:33]):
        raise BluetoothSetupError("ble_invalid_response")
    return BluetoothDeviceInfo(body[1:33].decode("ascii"), body[40:40 + body[39]].hex())


def validate_wifi(ssid: str, password: str) -> None:
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or "\x00" in ssid:
        raise BluetoothSetupError("ble_invalid_ssid")
    if not isinstance(password, str) or "\x00" in password:
        raise BluetoothSetupError("ble_invalid_wifi_password")
    length = len(password.encode("utf-8"))
    if length and not (8 <= length <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise BluetoothSetupError("ble_invalid_wifi_password")


def wifi_payload(ssid: str, password: str, address: str, *, mode=2) -> tuple[bytes, bytes]:
    """Encode 0x69 CONNECT with the app's BSSID fallback and random-code format.

The observed Q1 modules have region/function extensions disabled. Security and
channel 0 let the module detect the router. BLE MAC is the app's documented
fallback when the phone cannot obtain the router BSSID.
"""
    validate_wifi(ssid, password)
    if type(mode) is not int or mode not in (1, 2):
        raise BluetoothSetupError("ble_invalid_parameters")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
        raise BluetoothSetupError("ble_invalid_parameters")
    bssid = bytes.fromhex(address.replace(":", ""))
    ssid_bytes, password_bytes = ssid.encode("utf-8"), password.encode("utf-8")
    random_code = token_bytes(2) + md5(bssid + password_bytes).digest()[:14]
    # NetHome's existing mode remains 2; SmartHome's ordinary CONNECT is 1.
    # Security/channel=auto and region=0 for the observed Q1 advertisements.
    body = bytes([mode]) + bssid + \
        bytes([0, len(ssid_bytes), len(password_bytes)])
    return body + ssid_bytes + password_bytes + random_code + bytes(5), random_code


class MideaBluetoothSession:
    """One bounded BLE connection using HA's local adapters or active proxies."""

    def __init__(self, hass, discovery):
        self.hass = hass
        self.discovery = discovery
        self.client = None
        self._key = None
        self._sequence = 0
        self._buffer = FrameBuffer()
        self._queue = asyncio.Queue(maxsize=128)
        self._status_queue = asyncio.Queue(maxsize=128)
        self._closing = False
        self.stage = "connect"

    def _receive(self, _characteristic, data):
        try:
            for frame in self._buffer.feed(data):
                queue = self._status_queue if frame[4] == 0x0D else self._queue
                queue.put_nowait(frame)
        except (BluetoothSetupError, asyncio.QueueFull):
            self._buffer.buffer.clear()
            for queue in (self._queue, self._status_queue):
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(BluetoothSetupError("ble_invalid_response"))

    def _disconnected(self, _client):
        if not self._closing:
            for queue in (self._queue, self._status_queue):
                if not queue.full():
                    queue.put_nowait(BluetoothSetupError("ble_disconnected"))

    async def __aenter__(self):
        if self.discovery.protocol != 1:
            raise BluetoothSetupError("ble_unsupported_protocol")
        # Discovery history can outlive the scanner's actual connection path.
        # Ask HA's shared AUTO adapters/proxies for a fresh sweep before using
        # a remembered advertisement. Never start a separate BleakScanner.
        if not bluetooth.async_scanner_devices_by_address(
                self.hass, self.discovery.address, connectable=True):
            await bluetooth.async_request_active_scan(self.hass, 8)
        if not bluetooth.async_scanner_devices_by_address(
                self.hass, self.discovery.address, connectable=True):
            raise BluetoothSetupError("ble_not_connectable")
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.discovery.address, connectable=True)
        if device is None:
            raise BluetoothSetupError("ble_not_connectable")
        try:
            async with asyncio.timeout(45):
                self.client = await establish_connection(
                    # Resolve at connection time: HA replaces this class with
                    # its adapter/proxy dispatcher during Bluetooth startup.
                    # This module can load earlier through an account entry.
                    bleak.BleakClient, device, self.discovery.name,
                    disconnected_callback=self._disconnected, max_attempts=2,
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, self.discovery.address, connectable=True), pair=False,
                )
                self.stage = "subscribe"
                await self.client.start_notify(REPLY_UUID, self._receive)
                await self._handshake()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_args):
        self._closing = True
        try:
            if self.client is not None and self.client.is_connected:
                await asyncio.wait_for(self.client.disconnect(), 5)
        except (BleakError, OSError, TimeoutError):
            pass
        finally:
            self._key = None
            self._buffer.buffer.clear()
            for queue in (self._queue, self._status_queue):
                while not queue.empty():
                    queue.get_nowait()

    async def _send(self, command, body, key):
        self._sequence = (self._sequence + 1) % 256 or 1
        frame = encode_frame(self._sequence, command, body, key)
        for offset in range(0, len(frame), 20):
            await self.client.write_gatt_char(WRITE_UUID, frame[offset:offset + 20], response=True)

    async def _reply(self, command, key, *, prefix=None):
        async with asyncio.timeout(12):
            while True:
                queue = self._status_queue if command == 0x0D else self._queue
                frame = await queue.get()
                if isinstance(frame, Exception):
                    raise frame
                if frame[4] != command:
                    continue
                payload = decrypt(frame[5:-1], key)
                if prefix is not None and not payload.startswith(prefix):
                    continue
                if command != 0x0D:
                    self._sequence = frame[3]
                return payload

    async def _handshake(self):
        self.stage = "handshake_public_key"
        await self._send(0x01, b"\x01" + bytes(9), BOOTSTRAP)
        peer = await self._reply(0x01, BOOTSTRAP)
        if len(peer) != 66 or peer[:2] != b"\x01\x02":
            raise BluetoothSetupError("ble_handshake_failed")
        try:
            public = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), b"\x04" + peer[2:])
        except ValueError:
            raise BluetoothSetupError("ble_handshake_failed") from None
        private = ec.generate_private_key(ec.SECP256R1())
        key = private.exchange(ec.ECDH(), public)[:16]
        own_public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]
        self.stage = "handshake_proof"
        await self._send(0x01, b"\x02\x02" + own_public + encrypt(PROOF, key), BOOTSTRAP)
        ack = await self._reply(0x01, BOOTSTRAP)
        if len(ack) < 3 or ack[0] != 2 or ack[2] != 0:
            raise BluetoothSetupError("ble_handshake_failed")
        self._key = key

    async def device_info(self):
        self.stage = "device_info"
        await self._send(0x63, bytes(19), self._key)
        info = parse_device_info(await self._reply(0x63, self._key, prefix=b"\x00"))
        if (self.discovery.sn8 not in info.serial
                or info.serial[-8:-4].upper() != self.discovery.suffix):
            raise BluetoothSetupError("ble_identity_mismatch")
        return info

    async def configure_wifi(self, payload, mark_write_started):
        # Persist uncertainty in the flow before the first write. A timeout
        # after this point must not silently resend credentials on Retry.
        mark_write_started()
        await self._send(0x69, payload, self._key)
        ack = await self._reply(0x69, self._key)
        if not ack:
            raise BluetoothSetupError("ble_invalid_response")
        errors = {1: "ble_invalid_parameters", 2: "ble_auth_required",
                  5: "ble_unsupported_wifi", 6: "ble_already_bound"}
        if ack[0] not in (0, 3):
            raise BluetoothSetupError(errors.get(ack[0], "ble_wifi_rejected"))
        # The app marks ACK 3 as "already has Wi-Fi information". Neither ACK
        # alone establishes that a fresh cloud binding window has opened.
        return ack[0]

    async def wait_for_network(self, on_status=None):
        """Read progress notifications until cloud login or a clear failure."""
        async with asyncio.timeout(75):
            while True:
                try:
                    body = await self._reply(0x0D, self._key)
                except TimeoutError:
                    continue
                if len(body) < 3 or body[0] != 1:
                    continue
                if on_status is not None:
                    on_status(body[1], body[2])
                if body[2]:
                    errors = {1: "ble_wifi_not_found", 2: "ble_wifi_wrong_password",
                              3: "ble_wifi_dns", 13: "ble_wifi_dhcp"}
                    raise BluetoothSetupError(errors.get(
                        body[2], "ble_wifi_cloud_failed"))
                if body[1] == 4:
                    return


async def probe_device(hass, discovery):
    """Handshake and identify only; close the connection before showing Wi-Fi input."""
    session = MideaBluetoothSession(hass, discovery)
    try:
        async with session:
            return await session.device_info()
    except (BleakError, OSError, TimeoutError) as exc:
        _LOGGER.warning("Bluetooth identity check failed at %s (%s)",
                        session.stage, type(exc).__name__)
        raise BluetoothSetupError("ble_cannot_connect") from None
