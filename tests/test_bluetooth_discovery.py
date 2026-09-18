"""BLE advertisement decoding and discovery without provisioning writes."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.midea_connect.bluetooth_discovery import \
    parse_advertisement
from custom_components.midea_connect.const import DOMAIN

# Advertisement bytes in the layout observed through Home Assistant's Bluetooth API.
IDENTITY = bytes.fromhex("013030303030513142414337453444")
CAPABILITIES = bytes.fromhex("01010032b21a2b3c7e4e")


def advertisement(payload=IDENTITY, **kwargs):
    return SimpleNamespace(**{
        "address": "B2:1A:2B:3C:7E:4E", "name": "net", "rssi": -77,
        "manufacturer_data": {1704: payload}, "raw": None,
        "service_uuids": [], "source": "test-proxy", "connectable": True,
        **kwargs,
    })


@pytest.mark.parametrize("payload", [IDENTITY, IDENTITY + CAPABILITIES])
def test_identity_from_local_or_proxy_without_service_uuids(payload):
    found = parse_advertisement(advertisement(payload))
    assert found.name == "AC 7E4D"
    assert found.sn8 == "00000Q1B"
    assert found.protocol == 1
    assert found.device_type == "AC"


def test_commercial_ac_category_is_preserved_for_cloud_binding():
    found = parse_advertisement(advertisement(
        IDENTITY[:9] + b"CC" + IDENTITY[11:]))
    assert found.device_type == "CC"


def test_duplicate_manufacturer_records_use_raw_identity():
    raw = bytes([len(IDENTITY) + 3, 255, 168, 6]) + IDENTITY
    raw += bytes([len(CAPABILITIES) + 3, 255, 168, 6]) + CAPABILITIES
    assert parse_advertisement(advertisement(
        CAPABILITIES, raw=raw)).name == "AC 7E4D"


@pytest.mark.parametrize("payload", [b"", CAPABILITIES, IDENTITY[:10],
                                     b"\x03" + IDENTITY[1:],
                                     IDENTITY[:9] + b"FA" + IDENTITY[11:],
                                     b"\x01" + b"\xff" * 14])
def test_invalid_or_other_appliance_advertisement_is_ignored(payload):
    assert parse_advertisement(advertisement(payload, raw=b"\xffbad")) is None


async def test_bluetooth_discovery_deduplicates_without_creating_ac_entry(hass):
    with patch("custom_components.midea_connect.config_flow.Device.construct") as construct:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "bluetooth"}, data=advertisement())
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_found"
        assert not result["data_schema"].schema
        assert result["description_placeholders"] == {"name": "AC 7E4D"}
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "bluetooth"}, data=advertisement())
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_in_progress"
    assert not hass.config_entries.async_entries(DOMAIN)
    construct.assert_not_called()


async def test_nearby_picker_reads_shared_scanners_and_survives_existing_discovery(hass):
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "bluetooth"}, data=advertisement())
    with (
        patch("custom_components.midea_connect.bluetooth_flow.probe_device", return_value=SimpleNamespace(serial="0" * 32)) as probe,
        patch("custom_components.midea_connect.config_flow.bluetooth.async_scanner_count", return_value=1),
        patch("custom_components.midea_connect.config_flow.bluetooth.async_address_present",
              return_value=True),
        patch("custom_components.midea_connect.config_flow.bluetooth.async_scanner_devices_by_address",
              return_value=[object()]),
        patch("custom_components.midea_connect.config_flow.bluetooth.async_discovered_service_info",
              return_value=[advertisement(), advertisement(b"unrelated", address="other")]) as scan,
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "nearby_bluetooth"})
        assert result["type"] is FlowResultType.FORM
        assert result["data_schema"].schema["address"].container["B2:1A:2B:3C:7E:4E"] == "AC 7E4D · -77 dBm · Connection route available"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"address": "B2:1A:2B:3C:7E:4E"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.config_entries.flow._progress[result["flow_id"]]._ble_task
        probe.assert_awaited_once()
    assert result["step_id"] == "bluetooth_connect"
    assert not hass.config_entries.async_entries(DOMAIN)
    scan.assert_called_with(hass, connectable=False)


@pytest.mark.parametrize("scanners,reason", [(0, "no_bluetooth_scanner"), (1, "no_bluetooth_devices")])
async def test_bluetooth_unavailable_explains_reason(hass, scanners, reason):
    with (
        patch("custom_components.midea_connect.config_flow.bluetooth.async_scanner_count",
              return_value=scanners),
        patch("custom_components.midea_connect.config_flow.bluetooth.async_discovered_service_info", return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "nearby_bluetooth"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == reason
