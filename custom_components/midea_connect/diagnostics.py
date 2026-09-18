"""Diagnostics support for Midea Connect."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TOKEN
from homeassistant.core import HomeAssistant
from msmart.const import DeviceType
from msmart.device import AirConditioner as AC
from msmart.device import CommercialAirConditioner as CC

from .const import (CONF_ACCOUNT_PROVIDER, CONF_ENTRY_KIND, CONF_KEY, DOMAIN,
                    ENTRY_KIND_ACCOUNT, PROVIDER_NETHOME)

_REDACT = [
    CONF_KEY,
    CONF_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    "sessionId",
    "key"
]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    if config_entry.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT:
        return {"entry_kind": ENTRY_KIND_ACCOUNT,
                "account_provider": config_entry.data.get(CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME),
                "region_code": config_entry.data.get("region_code"),
                "credentials_saved": True}

    # Fetch coordinator from global data
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    device = coordinator.device

    # Get base device for basic info
    if device.type == DeviceType.AIR_CONDITIONER:
        base_info = super(AC, device._device).to_dict()
    elif device.type == DeviceType.COMMERCIAL_AC:
        base_info = super(CC, device._device).to_dict()

    feature_info = device.capabilities_dict()

    if hasattr(device, "enable_energy_usage_requests"):
        feature_info["enable_energy_usage_requests"] = device.enable_energy_usage_requests

    if hasattr(device, "_supported_properties"):
        feature_info["_supported_properties"] = device._supported_properties

    return {
        "config_entry": async_redact_data(config_entry.as_dict(), _REDACT),
        "lan_push": coordinator.push_diagnostics,
        "device": {
            # Dump basic device info
            **async_redact_data(base_info, _REDACT),

            # Dump supported features
            **feature_info
        }
    }
