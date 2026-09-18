"""Exercise idle reports through the actual encrypted V3 receive queue."""
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from msmart import crc8
from msmart.const import DeviceType
from msmart.frame import Frame, InvalidFrameException
from msmart.lan import LAN, Security, _LanProtocolV3, _Packet

from custom_components.midea_connect.coordinator import \
    MideaDeviceUpdateCoordinator
from custom_components.midea_connect.lan_push import (PushAirConditioner,
                                                      _NotifyingQueue)


def frame(body, frame_type=5):
    body = bytes(body)
    return Frame(DeviceType.AIR_CONDITIONER, frame_type).tobytes(
        body + bytes([crc8.calculate(body)]))


def state_frame():
    # An upstream msmart-ng status fixture: on, target 19.5 C, room 23.5 C.
    return frame(bytes.fromhex('c00193667f7f003c00000061570700550000000000000050'))


def environmental(humidity=56, indoor=100, outdoor=0xFF, decimal=0):
    body = bytearray(22)
    body[0] = 0xA1
    body[13:15] = bytes([indoor, outdoor])
    body[17:19] = bytes([humidity, decimal])
    return frame(body)


def encrypted_packet(protocol, raw):
    inner = _Packet.encode(1234, raw)
    pad = -(len(inner) + 2) % 16
    plain = bytes(2) + inner + bytes(pad)
    header = b'\x83\x70' + (len(inner) + pad + 32).to_bytes(2,
                                                            'big') + bytes([0x20, pad << 4 | 3])
    return header + Security.encrypt_aes_cbc(protocol._local_key, plain) + sha256(header + plain).digest()


def connected_device():
    device = PushAirConditioner(ip='127.0.0.1', port=6444, device_id=1234)
    protocol = _LanProtocolV3()
    protocol._peer = '127.0.0.1:6444'
    protocol._transport = MagicMock()
    protocol._transport.is_closing.return_value = False
    protocol._local_key = bytes(range(16))
    protocol._local_key_expiration = datetime.now(
        timezone.utc) + timedelta(hours=1)
    protocol._queue = _NotifyingQueue(device.push_lan.received)
    device.push_lan._protocol = protocol
    device.push_lan._protocol_version = 3
    device._online = True
    return device, protocol


@pytest.mark.parametrize(('raw_humidity', 'expected'), [(56, 56), (0, None), (255, None), (101, None)])
def test_partial_environmental_report_preserves_controls(raw_humidity, expected):
    device, _ = connected_device()
    device.apply_lan_report(state_frame())
    before = (device.power_state, device.target_temperature,
              device.operational_mode, device.fan_speed)
    assert device.apply_lan_report(environmental(raw_humidity, decimal=3))
    assert device.indoor_temperature == 25.3
    assert device.outdoor_temperature is None
    assert device.indoor_humidity == expected
    assert before == (device.power_state, device.target_temperature,
                      device.operational_mode, device.fan_speed)


def test_partial_report_rejects_bad_decimal_without_partial_mutation():
    device, _ = connected_device()
    before = device.to_dict()
    with pytest.raises(ValueError):
        device.apply_lan_report(environmental(outdoor=70, decimal=0xF3))
    assert device.to_dict() == before


def test_integrity_unknown_and_full_status():
    device, _ = connected_device()
    assert not device.apply_lan_report(b'\xaa')
    assert not device.apply_lan_report(frame(b'\xee\x00'))
    before = device.to_dict()
    raw = bytearray(state_frame())
    raw[-1] ^= 1
    with pytest.raises(InvalidFrameException):
        device.apply_lan_report(bytes(raw))
    assert device.to_dict() == before
    assert device.apply_lan_report(state_frame())
    assert device.target_temperature == 19.5
    assert device.power_state is True


async def test_queue_hook_preserves_packets_and_reinstalls_after_reconnect():
    device, _ = connected_device()
    lan = device.push_lan
    protocols = []

    async def connect(_self):
        protocol = _LanProtocolV3()
        protocol._queue.put_nowait(b'already-arrived')
        protocols.append(protocol)
        _self._protocol = protocol

    with patch.object(LAN, '_connect', connect):
        for _ in range(2):
            lan.received.clear()
            await lan._connect()
            assert lan.received.is_set()
            assert lan._protocol._queue.get_nowait() == b'already-arrived'
            lan.received.clear()
            lan._protocol._queue.put_nowait(b'new')
            assert lan.received.is_set()
            assert lan._protocol._queue.get_nowait() == b'new'
    assert protocols[0]._queue is not protocols[1]._queue


async def test_encrypted_fragmented_push_updates_ha_without_poll_or_timer_reset(hass):
    device, protocol = connected_device()
    coordinator = MideaDeviceUpdateCoordinator(hass, device)
    updated = asyncio.Event()
    unsub = coordinator.async_add_listener(updated.set)
    scheduled_poll = coordinator._unsub_refresh
    coordinator.async_start_push()
    try:
        packet = encrypted_packet(protocol, state_frame())
        with patch.object(device, 'refresh', AsyncMock()) as refresh, patch.object(device.push_lan, 'send', AsyncMock()) as send:
            protocol.data_received(packet[:4])
            assert not device.push_lan.received.is_set()
            protocol.data_received(packet[4:])
            await asyncio.wait_for(updated.wait(), 1)
            assert coordinator.device.target_temperature == 19.5
            assert coordinator.push_diagnostics['notification_frames_received'] == 1
            assert coordinator._unsub_refresh is scheduled_poll
            refresh.assert_not_called()
            send.assert_not_called()
    finally:
        unsub()
        await coordinator.async_shutdown()
    protocol._transport.close.assert_called_once()
    assert coordinator.push_diagnostics['listening'] is False


async def test_command_reader_wins_while_lock_held(hass):
    device, protocol = connected_device()
    coordinator = MideaDeviceUpdateCoordinator(hass, device)
    coordinator.async_start_push()
    try:
        async with coordinator._lock:
            protocol.data_received(encrypted_packet(protocol, state_frame()))
            await asyncio.sleep(0)
            assert coordinator.push_diagnostics['idle_frames_received'] == 0
            # Simulate the in-flight command consuming its own response.
            assert await device.push_lan._read(timeout=0) == state_frame()
        await asyncio.sleep(0)
        assert coordinator.push_diagnostics['idle_frames_received'] == 0
    finally:
        await coordinator.async_shutdown()


async def test_bad_frame_then_good_frame_and_duplicate(hass):
    device, protocol = connected_device()
    coordinator = MideaDeviceUpdateCoordinator(hass, device)
    updated = asyncio.Event()
    unsub = coordinator.async_add_listener(updated.set)
    coordinator.async_start_push()
    try:
        bad = bytearray(state_frame())
        bad[-1] ^= 1
        protocol.data_received(encrypted_packet(protocol, bytes(
            bad)) + encrypted_packet(protocol, state_frame()))
        await asyncio.wait_for(updated.wait(), 1)
        assert coordinator.push_diagnostics['invalid_frames'] == 1
        assert coordinator.push_diagnostics['transport_errors'] == 0
        updates = coordinator.push_diagnostics['state_updates']
        updated.clear()
        protocol.data_received(encrypted_packet(protocol, state_frame()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert coordinator.push_diagnostics['idle_frames_received'] == 2
        assert coordinator.push_diagnostics['state_updates'] == updates
        assert not updated.is_set()
    finally:
        unsub()
        await coordinator.async_shutdown()


async def test_transport_failure_marks_offline_leaves_polling_available(hass):
    device, protocol = connected_device()
    coordinator = MideaDeviceUpdateCoordinator(hass, device)
    updated = asyncio.Event()
    unsub = coordinator.async_add_listener(updated.set)
    coordinator.async_start_push()
    try:
        protocol._queue.put_nowait(OSError('connection lost'))
        await asyncio.wait_for(updated.wait(), 1)
        assert not coordinator.device.online
        assert coordinator.push_diagnostics['transport_errors'] == 1
        assert device.push_lan._protocol is None
        with patch.object(device, 'refresh', AsyncMock()) as refresh:
            await coordinator._async_update_data()
            refresh.assert_awaited_once()
        assert coordinator.update_interval.total_seconds() == 15
        # A regular poll reconnects; the listener follows the replacement queue.

        async def reconnect(_lan):
            _, replacement = connected_device()
            _lan._protocol = replacement

        with patch.object(LAN, '_connect', reconnect):
            await device.push_lan._connect()
        updated.clear()
        replacement = device.push_lan._protocol
        replacement.data_received(encrypted_packet(replacement, state_frame()))
        await asyncio.wait_for(updated.wait(), 1)
        assert coordinator.device.online
        assert coordinator.device.target_temperature == 19.5
    finally:
        unsub()
        await coordinator.async_shutdown()


def test_environmental_and_property_notifications_in_command_responses():
    device, _ = connected_device()
    from msmart.device.AC.command import Response

    # The normal refresh/apply path calls the same decoder while holding the lock.
    device._update_state(Response.construct(environmental(48)))
    assert device.indoor_humidity == 48
    assert device.apply_lan_report(
        frame(bytes.fromhex('b5 01 48 00 00 01 64')))
    assert int(device.rate_select) == 100


async def test_idle_report_does_not_overwrite_pending_user_change(hass):
    device, protocol = connected_device()
    coordinator = MideaDeviceUpdateCoordinator(hass, device)
    coordinator.device.target_temperature = 24
    updated = asyncio.Event()
    unsub = coordinator.async_add_listener(updated.set)
    coordinator.async_start_push()
    try:
        protocol.data_received(encrypted_packet(protocol, state_frame()))
        await asyncio.wait_for(updated.wait(), 1)
        assert device.target_temperature == 19.5
        assert coordinator.device.target_temperature == 24
    finally:
        unsub()
        await coordinator.async_shutdown()
