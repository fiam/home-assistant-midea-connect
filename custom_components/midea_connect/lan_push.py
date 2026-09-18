"""Idle LAN reports for the pinned msmart-ng 2026.9.0 AC implementation.

The library owns encryption, framing, authentication and reconnection. This
per-device adapter signals complete packets arriving in its existing V3 queue.
The coordinator drains that queue only while holding its command/poll lock, so
there are never competing readers or a second connection to the appliance.
"""
from __future__ import annotations

import asyncio

from msmart.device import AirConditioner
from msmart.device.AC.command import (CapabilitiesResponse, PropertiesResponse,
                                      Response)
from msmart.frame import Frame
from msmart.lan import LAN, _LanProtocolV3


class _NotifyingQueue(asyncio.Queue):
    def __init__(self, received: asyncio.Event):
        super().__init__()
        self._received = received

    def put_nowait(self, item):
        super().put_nowait(item)
        self._received.set()


class PushLAN(LAN):
    """Notify the coordinator without consuming requests' responses."""

    def __init__(self, ip: str, port: int, device_id: int):
        super().__init__(ip, port, device_id)
        self.received = asyncio.Event()

    async def _connect(self):
        await super()._connect()
        if not isinstance(self._protocol, _LanProtocolV3):
            return
        # _connect completes before authentication/command reading begins.
        # Preserve anything that arrived during connection establishment.
        old = self._protocol._queue
        queue = _NotifyingQueue(self.received)
        self._protocol._queue = queue
        while not old.empty():
            queue.put_nowait(old.get_nowait())


class PushAirConditioner(AirConditioner):
    """AC with the same control protocol and support for partial push reports."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lan = PushLAN(self.ip, self.port, self.id)

    @property
    def push_lan(self) -> PushLAN:
        return self._lan

    def _update_state(self, res: Response) -> None:
        if type(res) is Response and res.id == 0xA1:
            # A1 is an environmental report, not a full C0 control snapshot.
            # Offsets match NetHome/SmartHome Lua and midea-lan's XA1Body.
            body = res.payload
            if len(body) < 18:
                raise ValueError("Short environmental report")
            decimal = body[18] if len(body) > 20 else 0
            indoor = _temperature(body[13], decimal & 0x0F)
            outdoor = _temperature(body[14], decimal >> 4)
            self._indoor_temperature = indoor
            self._outdoor_temperature = outdoor
            humidity = body[17]
            self._indoor_humidity = humidity if 0 < humidity <= 100 else None
            return
        if type(res) is Response and res.id == 0xB5:
            # B5 with a notification frame type contains property records;
            # query-type B5 is CapabilitiesResponse and must stay separate.
            res = PropertiesResponse(memoryview(res.payload))
        super()._update_state(res)

    def apply_lan_report(self, raw: bytes) -> bool:
        """Apply a validated idle report; return whether it carries known state."""
        if len(raw) < 13 or raw[1] != len(raw) - 1 or raw[9] not in (2, 3, 4, 5):
            return False
        Frame.validate(memoryview(raw), self.type)
        response = Response.construct(raw)
        if isinstance(response, CapabilitiesResponse):
            return False
        if type(response) is Response and response.id not in (0xA1, 0xB5):
            return False
        self._update_state(response)
        self._online = True
        return True


def _temperature(integer: int, decimal: int) -> float | None:
    if integer == 0xFF:
        return None
    if decimal > 9:
        raise ValueError("Invalid temperature decimal")
    value = (integer - 50) / 2
    if not decimal:
        return value
    return int(value) + (-decimal if value < 0 else decimal) / 10
