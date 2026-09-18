"""Identify Midea AC setup advertisements without connecting to the device.

The v1/v2 identity layout comes from NetHome Plus's advertisement parser and
captures from the owner's controllers. BLE versions are unrelated to LAN V3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

MANUFACTURER_ID = 0x06A8


@dataclass(frozen=True)
class MideaBluetoothDiscovery:
    """Identity received through a local adapter or HA Bluetooth proxy."""

    address: str
    sn8: str
    suffix: str
    protocol: int
    rssi: int
    device_type: str = "AC"

    @property
    def name(self) -> str:
        return f"AC {self.suffix}"


def parse_advertisement(info: BluetoothServiceInfoBleak) -> MideaBluetoothDiscovery | None:
    """Accept only a supported AC identity, including merged or raw records."""
    candidates = [info.manufacturer_data.get(MANUFACTURER_ID, b"")]
    # Backends can retain only the final record for a duplicate manufacturer
    # ID. HA's raw advertisement can preserve the separate identity record.
    raw = getattr(info, "raw", None) or b""
    offset = 0
    while offset < len(raw):
        size = raw[offset]
        end = offset + size + 1
        if size == 0 or end > len(raw):
            break
        record = raw[offset + 1:end]
        if record[:3] == b"\xff\xa8\x06":
            candidates.append(record[3:])
        offset = end
    for value in candidates:
        if len(value) < 15 or value[0] not in (1, 2):
            continue
        if not re.fullmatch(rb"[A-Za-z0-9]{8}(?:AC|CC)[0-9A-Fa-f]{4}", value[1:15]):
            continue
        return MideaBluetoothDiscovery(
            address=info.address, sn8=value[1:9].decode("ascii"),
            suffix=value[11:15].decode("ascii").upper(),
            protocol=value[0], rssi=info.rssi,
            device_type=value[9:11].decode("ascii"),
        )
    return None
