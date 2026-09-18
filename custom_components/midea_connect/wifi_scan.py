"""Optional host Wi-Fi scan; AC setup never requires a host Wi-Fi adapter."""
from __future__ import annotations

import asyncio
import sys


async def async_scan_networks():
    """Use kube4ha's optional D-Bus service, falling back to manual SSID entry."""
    if sys.platform != "linux":
        return []
    bus = None
    networks = {}
    try:
        from dbus_fast import BusType, Message, MessageFlag, MessageType
        from dbus_fast.aio import MessageBus

        async with asyncio.timeout(15):
            bus = MessageBus(bus_type=BusType.SYSTEM)
            await bus.connect()
            reply = await bus.call(Message(
                destination="io.github.fiam.Kube4ha.Wifi",
                path="/io/github/fiam/Kube4ha/Wifi",
                interface="io.github.fiam.Kube4ha.Wifi1", member="Scan",
                flags=MessageFlag.NO_AUTOSTART,
            ))
            if reply.message_type != MessageType.METHOD_RETURN or reply.signature != "aa{sv}":
                return []
            for properties in reply.body[0]:
                try:
                    ssid = properties["ssid"].value
                    frequency = properties["frequency"].value
                    strength = properties["signal"].value
                    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
                        continue
                    if not 2400 <= frequency <= 2500 or not -127 <= strength <= 0:
                        continue
                except (KeyError, TypeError, AttributeError, UnicodeError):
                    continue
                networks[ssid] = max(networks.get(ssid, -127), strength)
    except Exception:
        # Missing D-Bus, helper, permissions, radio, or scan results must not
        # block setup. Never log host responses or network identities.
        return []
    finally:
        if bus is not None:
            bus.disconnect()
    return sorted(networks, key=lambda ssid: (-networks[ssid], ssid.casefold()))
