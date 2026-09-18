"""Optional scanning must not become a requirement for AC setup."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dbus_fast import MessageType, Variant

from custom_components.midea_connect.wifi_scan import async_scan_networks


def network(ssid, frequency=2412, signal=-60):
    return {"ssid": Variant("s", ssid), "frequency": Variant("u", frequency),
            "signal": Variant("n", signal)}


@pytest.fixture
def bus():
    bus = MagicMock()
    bus.connect = AsyncMock(return_value=bus)
    bus.call = AsyncMock()
    with patch("custom_components.midea_connect.wifi_scan.sys.platform", "linux"), \
            patch("dbus_fast.aio.MessageBus", return_value=bus):
        yield bus


async def test_filters_5ghz_hidden_and_bad_records_and_deduplicates(bus):
    bus.call.return_value = SimpleNamespace(message_type=MessageType.METHOD_RETURN,
                                            signature="aa{sv}", body=[[
                                                network(
                                                    "Home", signal=-80), network("Home", signal=-40),
                                                network(
                                                    "Guest", signal=-70), network("Only5GHz", 5180),
                                                network(""), network(
                                                    "é" * 17), {},
                                            ]])
    assert await async_scan_networks() == ["Home", "Guest"]
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize("failure", [OSError(), TimeoutError(), ImportError()])
async def test_unavailable_bus_or_wifi_keeps_manual_setup_available(bus, failure):
    bus.connect.side_effect = failure
    assert await async_scan_networks() == []
    bus.disconnect.assert_called_once()


async def test_absent_service_and_ethernet_only_host_return_empty_list(bus):
    bus.call.return_value = SimpleNamespace(message_type=MessageType.ERROR)
    assert await async_scan_networks() == []
    bus.call.return_value = SimpleNamespace(message_type=MessageType.METHOD_RETURN,
                                            signature="aa{sv}", body=[[]])
    assert await async_scan_networks() == []


async def test_cancel_during_scan_disconnects(bus):
    bus.call.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await async_scan_networks()
    bus.disconnect.assert_called_once()
