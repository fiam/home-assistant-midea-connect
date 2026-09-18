"""Config flow for Midea Connect."""
from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import httpx
import voluptuous as vol
import yaml
from homeassistant.components import bluetooth
from homeassistant.config_entries import (ConfigEntry, ConfigFlow,
                                          ConfigFlowResult, OptionsFlow)
from homeassistant.const import (CONF_COUNTRY_CODE, CONF_HOST, CONF_ID,
                                 CONF_PORT, CONF_TOKEN, DEGREE, UnitOfTime)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import httpx_client
from homeassistant.helpers.selector import (NumberSelector,
                                            NumberSelectorConfig,
                                            NumberSelectorMode, SelectSelector,
                                            SelectSelectorConfig,
                                            SelectSelectorMode, TextSelector,
                                            TextSelectorConfig,
                                            TextSelectorType)
from msmart.base_device import Device
from msmart.const import DeviceType
from msmart.device import AirConditioner as AC
from msmart.device import CommercialAirConditioner as CC
from msmart.discover import Discover
from msmart.lan import AuthenticationError, ProtocolError

from .account_device import DeviceSetupError, acquire_credentials
from .account_flow import AccountFlowMixin
from .bluetooth_discovery import parse_advertisement
from .bluetooth_flow import BluetoothFlowMixin
from .const import (CONF_ACCOUNT_ENTRY_ID, CONF_BEEP,
                    CONF_CAPABILITY_OVERRIDES, CONF_DEFAULT_CLOUD_COUNTRY,
                    CONF_DEVICE_TYPE, CONF_ENERGY_DATA_FORMAT,
                    CONF_ENERGY_DATA_SCALE, CONF_ENERGY_SENSOR,
                    CONF_ENTRY_KIND, CONF_FAN_SPEED_STEP, CONF_KEY,
                    CONF_MAX_CONNECTION_LIFETIME,
                    CONF_MERGE_CAPABILITY_OVERRIDES, CONF_POWER_SENSOR,
                    CONF_SWING_ANGLE_RTL, CONF_TEMP_STEP, CONF_UPDATE_INTERVAL,
                    CONF_USE_FAN_ONLY_WORKAROUND, CONF_WORKAROUNDS, DOMAIN,
                    ENTRY_KIND_ACCOUNT, UPDATE_INTERVAL, EnergyFormat)
from .credential_backup import export_credentials, import_credentials

_LOGGER = logging.getLogger(__name__)

_DEFAULT_OPTIONS = {
    CONF_UPDATE_INTERVAL: UPDATE_INTERVAL,
    CONF_TEMP_STEP: 1.0,
    CONF_MAX_CONNECTION_LIFETIME: None,
    CONF_SWING_ANGLE_RTL: False,
    CONF_CAPABILITY_OVERRIDES: "",
    CONF_MERGE_CAPABILITY_OVERRIDES: True
}

_DEFAULT_AC_OPTIONS = {
    CONF_BEEP: True,
    CONF_FAN_SPEED_STEP: 1,
    CONF_ENERGY_SENSOR: {
        CONF_ENERGY_DATA_FORMAT: EnergyFormat.BCD,
        CONF_ENERGY_DATA_SCALE: 1.0
    },
    CONF_POWER_SENSOR: {
        CONF_ENERGY_DATA_FORMAT: EnergyFormat.BCD,
        CONF_ENERGY_DATA_SCALE: 1.0
    },
    CONF_WORKAROUNDS: {
        CONF_USE_FAN_ONLY_WORKAROUND: False,
    }
}


class MideaConfigFlow(BluetoothFlowMixin, AccountFlowMixin, ConfigFlow, domain=DOMAIN):
    """Config flow for Midea Connect."""

    VERSION = 1
    MINOR_VERSION = 7

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle a config flow initialized by the user."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["nearby_bluetooth", "account", "advanced"],
        )

    async def async_step_advanced(self, user_input=None):
        return self.async_show_menu(step_id="advanced", menu_options=[
            "manual", "restore", "account_device"])

    async def async_step_nearby_bluetooth(self, user_input=None) -> ConfigFlowResult:
        """Refresh shared HA discovery without connecting or provisioning."""
        if user_input:
            action = user_input.get("address")
            if action in ("discover", "manual"):
                return await getattr(self, f"async_step_{action}")(None)
        configured = {e.data.get("bluetooth_address")
                      for e in self._async_current_entries()}
        scanners = bluetooth.async_scanner_count(self.hass, connectable=False)
        devices = {}
        if scanners:
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=False):
                if (info.address not in configured and parse_advertisement(info)
                        and not self._advertisement_configured(parse_advertisement(info))
                        and bluetooth.async_address_present(self.hass, info.address, connectable=False)):
                    if info.address not in devices or info.rssi > devices[info.address].rssi:
                        devices[info.address] = info
        if user_input and (info := devices.get(user_input.get("address"))):
            return await self.async_step_bluetooth(info)
        labels = {}
        for info in sorted(devices.values(), key=lambda item: item.rssi, reverse=True):
            discovery = parse_advertisement(info)
            reachable = bluetooth.async_scanner_devices_by_address(
                self.hass, info.address, connectable=True)
            route = "Connection route available" if reachable else "No Bluetooth connection route"
            labels[info.address] = f"{discovery.name} · {discovery.rssi} dBm · {route}"
        labels.update(refresh="Refresh nearby ACs", discover="Find ACs on the LAN",
                      manual="Enter connection details")
        errors = {}
        if not scanners:
            errors["base"] = "no_bluetooth_scanner"
        elif not devices:
            errors["base"] = "no_bluetooth_devices"
        elif user_input and user_input.get("address") != "refresh":
            errors["base"] = "bluetooth_disappeared"
        return self.async_show_form(step_id="nearby_bluetooth", errors=errors,
                                    data_schema=vol.Schema({vol.Required("address"): vol.In(labels)}))

    def _advertisement_configured(self, discovery):
        for entry in self._async_current_entries():
            if entry.data.get("bluetooth_address") == discovery.address:
                return True
            serial = entry.data.get("sn", "")
            if (isinstance(serial, str) and len(serial) == 32 and
                    discovery.sn8 in serial and serial[-8:-4].upper() == discovery.suffix):
                return True
        return False

    async def async_step_bluetooth(self, discovery_info: bluetooth.BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Surface supported Midea advertisements in Home Assistant discovery."""
        if (discovery := parse_advertisement(discovery_info)) is None:
            return self.async_abort(reason="unsupported_device")
        if self._advertisement_configured(discovery):
            return self.async_abort(reason="already_configured")
        # A discovery identity only: real AC entries still need verified LAN
        # credentials. Do not create an unusable entry for a BLE advertisement.
        await self.async_set_unique_id(
            f"ble:{discovery.address}", raise_on_progress=self.source == "bluetooth")
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": discovery.name}
        self._bluetooth_name = discovery.name
        self._bluetooth_discovery = discovery
        if self.source == "bluetooth":
            # Background discovery only creates the card. HA advances this
            # placeholder when the user clicks Add, starting the probe then.
            return self.async_show_form(
                step_id="bluetooth_found", data_schema=vol.Schema({}),
                description_placeholders={"name": discovery.name},
            )
        # Selection in the nearby picker is already an explicit Add action.
        return await self.async_step_bluetooth_connect()

    async def async_step_bluetooth_found(self, user_input=None) -> ConfigFlowResult:
        """Start connecting when the user opens a discovered AC's flow."""
        return await self.async_step_bluetooth_connect()

    async def async_step_account_device(self, user_input=None) -> ConfigFlowResult:
        """Add or recover an AC's credentials using a saved setup account."""
        if self._bluetooth_discovery is not None:
            return await self.async_step_bluetooth_connect()
        if self._setup_provider is None:
            self._provider_return = "account_device"
            return await self.async_step_setup_provider()
        accounts = self._setup_accounts()
        if not accounts:
            return await self.async_step_setup_account_missing()
        errors = {}
        if user_input is not None:
            account = accounts.get(user_input[CONF_ACCOUNT_ENTRY_ID])
            if account is None:
                return self.async_abort(reason="no_setup_accounts")
            try:
                device = await acquire_credentials(
                    self._get_async_client(), account.data, user_input[CONF_HOST])
            except DeviceSetupError as exc:
                errors["base"] = str(exc)
            except OSError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(device.id))
                existing = next((entry for entry in self._async_current_entries()
                                 if entry.unique_id == str(device.id)), None)
                if existing:
                    return self.async_update_reload_and_abort(existing, data_updates={
                        CONF_HOST: device.ip, CONF_PORT: device.port,
                        CONF_TOKEN: device.token, CONF_KEY: device.key,
                        CONF_ACCOUNT_ENTRY_ID: account.entry_id,
                    }, reason="account_tokens_updated")
                self._setup_account_entry_id = account.entry_id
                return await self._create_entry_from_device(device)
        return self.async_show_form(step_id="account_device", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_ACCOUNT_ENTRY_ID): vol.In({
                                            key: entry.title for key, entry in accounts.items()}),
                                        vol.Required(CONF_HOST, default=getattr(getattr(self, "_selected_lan_device", None), "ip", "")): cv.string,
                                    }))

    async def async_step_restore(self, user_input=None) -> ConfigFlowResult:
        """Restore through the manual LAN path, with no cloud fallback."""
        errors = {}
        if user_input is not None:
            try:
                config = import_credentials(user_input.get("credentials"))
                if host := user_input.get(CONF_HOST):
                    config[CONF_HOST] = host
            except ValueError:
                errors["base"] = "invalid_credential_backup"
            else:
                # Reuse duplicate detection, local authentication, state validation
                # and config-entry storage. Do not call Discover.connect here.
                try:
                    return await self.async_step_manual(config)
                except (OSError, ProtocolError):
                    errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="restore", errors=errors,
            data_schema=vol.Schema({
                vol.Required("credentials"): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_HOST): cv.string,
            }),
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the discovery step of config flow."""
        errors = {}

        if user_input is not None:
            country_code = user_input.get(
                CONF_COUNTRY_CODE, CONF_DEFAULT_CLOUD_COUNTRY)

            # If host was not provided, discover all devices
            if not (host := user_input.get(CONF_HOST)):
                return await self.async_step_pick_device(country_code=country_code)

            # Attempt to find specified device
            device = await Discover.discover_single(
                host,
                auto_connect=False,
                timeout=2,
                region=country_code,
                get_async_client=self._get_async_client
            )

            if device is None:
                errors["base"] = "device_not_found"
            elif device.type not in [DeviceType.AIR_CONDITIONER, DeviceType.COMMERCIAL_AC]:
                errors["base"] = "unsupported_device"
            else:
                # Attempt connection
                return await self._attempt_auto_connection(device)

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema({
                vol.Optional(CONF_HOST, default=""): str,

            }), user_input)

        return self.async_show_form(
            step_id="discover",
            data_schema=data_schema,
            errors=errors
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None,
        *,
        country_code: str = CONF_DEFAULT_CLOUD_COUNTRY
    ) -> ConfigFlowResult:
        """Handle the pick device step of config flow."""

        if user_input is not None:
            # Find selected device
            device = next(
                dev
                for dev in self._discovered_devices
                if dev.id == user_input[CONF_ID]
            )

            if device:
                # Attempt connection
                return await self._attempt_auto_connection(device)

        # Create a set of already configured devices by ID
        configured_devices = {
            entry.unique_id for entry in self._async_current_entries()
        }

        # Discover all devices
        self._discovered_devices = await Discover.discover(
            auto_connect=False,
            timeout=2,
            region=country_code,
            get_async_client=self._get_async_client
        )

        # Create a dict of supported devices
        supported_devices = {
            device.id: f"{device.name} - {device.id} ({device.ip})"
            for device in self._discovered_devices
            if device.type in [DeviceType.AIR_CONDITIONER, DeviceType.COMMERCIAL_AC]
        }

        # No supported devices found
        if len(supported_devices) == 0:
            return self.async_abort(reason="no_devices_found")

        # Show device picker if new devices found
        new_devices = {
            dev_id: name
            for dev_id, name in supported_devices.items()
            if str(dev_id) not in configured_devices
        }
        if len(new_devices):
            return self.async_show_form(
                step_id="pick_device",
                data_schema=vol.Schema({
                    vol.Required(CONF_ID): vol.In(new_devices)
                })
            )

        # No new devices, show existing devices
        return self.async_abort(
            reason="already_configured_devices_found",
            description_placeholders={
                "devices": "\n".join(
                    f"- {name}"
                    for name in supported_devices.values()
                )
            }
        )

    async def async_step_manual(self, user_input=None) -> ConfigFlowResult:
        """Handle the manual step of config flow."""
        errors = {}

        if user_input is not None:
            # Get device ID from user input
            id = int(user_input.get(CONF_ID))

            # Check if device has already been configured
            await self.async_set_unique_id(str(id))
            self._abort_if_unique_id_configured()

            # Validate the hex format of certain fields
            for field in [CONF_TOKEN, CONF_KEY]:
                if input := user_input.get(field):
                    try:
                        bytes.fromhex(input)
                    except (ValueError, TypeError):
                        errors[field] = "invalid_hex_format"

            if not errors:
                # Attempt a connection to see if config is valid
                device = await self._test_manual_connection(user_input)

                if not device or device.online == False:
                    # Indicate a connection could not be made
                    errors["base"] = "cannot_connect"
                elif device and device.supported == False:
                    # Indicate unsupported device type
                    errors["base"] = "unsupported_device"
                else:
                    # Create entry from valid device
                    return await self._create_entry_from_device(device)

        user_input = user_input or {}

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema({
                vol.Required(CONF_ID): cv.string,
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_PORT, default=6444): cv.port,
                vol.Required(CONF_DEVICE_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=[f"{e.value:X}" for e in
                                 [DeviceType.AIR_CONDITIONER, DeviceType.COMMERCIAL_AC]],
                        translation_key="device_type",
                        mode=SelectSelectorMode.DROPDOWN,
                    ),
                ),
                vol.Optional(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(CONF_KEY): cv.string
            }), user_input)

        return self.async_show_form(
            step_id="manual",
            data_schema=data_schema,
            errors=errors
        )

    async def async_step_reconfigure(self, user_input) -> ConfigFlowResult:
        """Handle the reconfiguration step of config flow."""
        if self._get_reconfigure_entry().data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT:
            return await self.async_step_account_password(user_input)
        errors = {}

        if user_input is not None:
            # Validate the hex format of certain fields
            for field in [CONF_TOKEN, CONF_KEY]:
                if input := user_input.get(field):
                    try:
                        bytes.fromhex(input)
                    except (ValueError, TypeError):
                        errors[field] = "invalid_hex_format"

            if not errors:
                config_entry = self._get_reconfigure_entry()

                # Copy ID from existing entry
                user_input[CONF_ID] = config_entry.data[CONF_ID]

                # Convert type to hex representation
                user_input[CONF_DEVICE_TYPE] = f"{config_entry.data[CONF_DEVICE_TYPE]:X}"

                # Ensure key & token are in dict
                user_input.setdefault(CONF_KEY, None)
                user_input.setdefault(CONF_TOKEN, None)

                # Attempt a connection to see if config is valid
                device = await self._test_manual_connection(user_input)

                if not device or device.online == False:
                    # Indicate a connection could not be made
                    errors["base"] = "cannot_connect"
                elif device and device.supported == False:
                    # Indicate unsupported device type
                    errors["base"] = "unsupported_device"
                else:
                    # Update entry
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(),
                        data_updates={
                            CONF_DEVICE_TYPE: device.type,
                            CONF_ID: device.id,
                            CONF_HOST: device.ip,
                            CONF_PORT: device.port,
                            CONF_TOKEN: device.token,
                            CONF_KEY: device.key,
                        },
                    )

        # Use existing config entry data if no user input
        user_input = user_input or self._get_reconfigure_entry().data

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema({
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_PORT, default=6444): cv.port,
                vol.Optional(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(CONF_KEY): cv.string
            }), user_input)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
            errors=errors
        )

    def _get_async_client(self, *args, **kwargs) -> httpx.AsyncClient:
        """Create an httpx AsyncClient in a HA friendly way."""
        return httpx_client.get_async_client(self.hass, *args, **kwargs)

    async def _test_manual_connection(self, config) -> AC | CC | None:
        DEVICE_TYPES = {
            "AC": DeviceType.AIR_CONDITIONER,
            "CC": DeviceType.COMMERCIAL_AC
        }

        # Construct the device
        device = Device.construct(
            type=DEVICE_TYPES[config.get(CONF_DEVICE_TYPE).upper()],
            ip=config.get(CONF_HOST),
            port=config.get(CONF_PORT),
            device_id=int(config.get(CONF_ID)),
        )

        # Ensure device is a supported type
        assert isinstance(device, (AC, CC))

        # Authenticate with device as needed
        token = config.get(CONF_TOKEN)
        key = config.get(CONF_KEY)
        if token and key:
            try:
                await device.authenticate(token, key)
            except AuthenticationError:
                return None

        # Attempt to refresh device state
        await device.refresh()

        return device

    async def _attempt_auto_connection(self, device: Device) -> ConfigFlowResult:
        # Check if device has already been configured
        await self.async_set_unique_id(str(device.id))
        self._abort_if_unique_id_configured()

        self._selected_lan_device = device
        return await self.async_step_lan_credentials()

    async def async_step_lan_credentials(self, user_input=None):
        return self.async_show_menu(step_id="lan_credentials", menu_options=[
            "account_device", "restore", "manual"])

    async def _create_entry_from_device(self, device) -> ConfigFlowResult:
        # Save the device into global data
        self.hass.data.setdefault(DOMAIN, {})

        # Populate config data
        data = {
            CONF_DEVICE_TYPE: device.type,
            CONF_ID: device.id,
            CONF_HOST: device.ip,
            CONF_PORT: device.port,
            CONF_TOKEN: device.token,
            CONF_KEY: device.key,
        }

        serial = getattr(device, "sn", None)
        if isinstance(serial, str) and len(serial) == 32:
            data["sn"] = serial
        if account_id := getattr(self, "_setup_account_entry_id", None):
            data[CONF_ACCOUNT_ENTRY_ID] = account_id
        if self._bluetooth_discovery is not None and self._ble_device is not None:
            data.update(bluetooth_address=self._bluetooth_discovery.address,
                        sn=self._ble_state.serial, cloud_appliance_id=self._ble_state.appliance_code)

        # Build default options based on device type
        if device.type == DeviceType.AIR_CONDITIONER:
            default_options = _DEFAULT_OPTIONS | _DEFAULT_AC_OPTIONS
        else:
            default_options = _DEFAULT_OPTIONS

        # Create a config entry with the config data and default options
        title = f"AC {device.id}"
        if isinstance(serial, str) and len(serial) == 32:
            title = f"AC {serial[-8:-4].upper()}"
        if self._bluetooth_discovery:
            title = self._bluetooth_discovery.name
        return self.async_create_entry(title=title, data=data, options=default_options,
                                       description="local_device")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MideaOptionsFlow:
        """Create the options flow."""
        return MideaOptionsFlow()


class MideaOptionsFlow(OptionsFlow):
    """Options flow from Midea Connect."""

    _BASE_SCHEMA = vol.Schema(
        {
            vol.Optional(CONF_UPDATE_INTERVAL): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=30,
                    step=1,
                    unit_of_measurement=UnitOfTime.SECONDS,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(CONF_SWING_ANGLE_RTL): cv.boolean,
            vol.Optional(CONF_TEMP_STEP): NumberSelector(
                NumberSelectorConfig(
                    min=.5,
                    max=5,
                    step=.5,
                    unit_of_measurement=DEGREE
                )
            ),
            vol.Optional(CONF_MAX_CONNECTION_LIFETIME): vol.All(
                vol.Coerce(int),
                vol.Range(min=UPDATE_INTERVAL)
            ),
            vol.Optional(CONF_CAPABILITY_OVERRIDES):  TextSelector(
                TextSelectorConfig(
                    multiline=True,
                    type=TextSelectorType.TEXT
                )
            ),
            vol.Optional(CONF_MERGE_CAPABILITY_OVERRIDES): cv.boolean,
        }
    )

    _ENERGY_SENSOR_SCHEMA = section(
        vol.Schema(
            {
                vol.Required(CONF_ENERGY_DATA_FORMAT): SelectSelector(
                    SelectSelectorConfig(
                        options=[e.value for e in
                                 [EnergyFormat.BCD, EnergyFormat.BINARY]],
                        translation_key="energy_data_format",
                        mode=SelectSelectorMode.DROPDOWN,
                    ),
                ),
                vol.Required(CONF_ENERGY_DATA_SCALE): NumberSelector(
                    NumberSelectorConfig(
                        min=.001, step="any", mode=NumberSelectorMode.BOX)
                ),
            }
        ),
        {"collapsed": True}
    )

    _AC_OPTION_SCHEMA = vol.Schema(
        {
            vol.Optional(CONF_BEEP): cv.boolean,
            vol.Optional(CONF_FAN_SPEED_STEP): NumberSelector(
                NumberSelectorConfig(min=1, max=20, step=1)
            ),
            vol.Optional(CONF_ENERGY_SENSOR): _ENERGY_SENSOR_SCHEMA,
            vol.Optional(CONF_POWER_SENSOR): _ENERGY_SENSOR_SCHEMA,
            vol.Optional(CONF_WORKAROUNDS): section(
                vol.Schema({
                    vol.Optional(CONF_USE_FAN_ONLY_WORKAROUND): cv.boolean
                }),
                {"collapsed": True},
            )
        }
    )

    _CC_OPTION_SCHEMA = vol.Schema({})

    _DEVICE_SCHEMAS = {
        DeviceType.AIR_CONDITIONER: _AC_OPTION_SCHEMA,
        DeviceType.COMMERCIAL_AC: _CC_OPTION_SCHEMA,
    }

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Offer settings and an explicit credential view for this AC."""
        if self.config_entry.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT:
            return self.async_abort(reason="account_options_reconfigure")
        menu = ["connection_details", "settings"]
        if self.config_entry.data.get(CONF_TOKEN) and self.config_entry.data.get(CONF_KEY):
            menu.append("show_credentials")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_connection_details(self, user_input=None):
        coordinator = self.hass.data.get(
            DOMAIN, {}).get(self.config_entry.entry_id)
        account = self.hass.config_entries.async_get_entry(
            self.config_entry.data.get(CONF_ACCOUNT_ENTRY_ID, ""))
        return self.async_show_form(step_id="connection_details", data_schema=vol.Schema({}),
                                    description_placeholders={
            "host": str(self.config_entry.data.get(CONF_HOST, "Unknown")),
            "account": (BluetoothFlowMixin._ble_account_label(account) if account else
                        "Saved local credentials (no setup account)"),
            "last_seen": getattr(coordinator, "last_local_success", None) or "Not yet verified",
            "updates": ("LAN push with polling fallback" if coordinator and
                        coordinator.push_diagnostics["enabled"] else "LAN polling"),
        })

    async def async_step_show_credentials(self, user_input=None) -> ConfigFlowResult:
        """Read this AC's saved credentials without connecting or changing options."""
        if self.config_entry.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT:
            return self.async_abort(reason="account_options_reconfigure")
        if user_input is not None:
            return self.async_abort(reason="credentials_closed")
        if not self.config_entry.data.get(CONF_TOKEN) or not self.config_entry.data.get(CONF_KEY):
            return self.async_abort(reason="credentials_unavailable")
        try:
            credentials = export_credentials(dict(self.config_entry.data))
        except ValueError:
            return self.async_abort(reason="credentials_unavailable")
        return self.async_show_form(
            step_id="show_credentials", data_schema=vol.Schema({}),
            description_placeholders={"credentials": credentials},
        )

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        """Edit this AC's existing advanced settings."""
        if self.config_entry.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT:
            return self.async_abort(reason="account_options_reconfigure")
        errors = {}

        if user_input is not None:
            if yaml_input := user_input.get(CONF_CAPABILITY_OVERRIDES):
                try:
                    overrides = yaml.safe_load(yaml_input)
                    if not isinstance(overrides, dict):
                        raise ValueError()

                except yaml.YAMLError as e:
                    _LOGGER.error(
                        "Failed to parse capability overrides YAML: %s", e)
                    errors[CONF_CAPABILITY_OVERRIDES] = "override_yaml_parse_error"
                except ValueError as e:
                    _LOGGER.error("Expected dict for capability overrides.")
                    errors[CONF_CAPABILITY_OVERRIDES] = "override_yaml_format_error"

            if not errors:
                return self.async_create_entry(data=user_input)

        # Get options schema based on device type
        device_type = self.config_entry.data.get(CONF_DEVICE_TYPE)
        device_schema = {}

        if schema := self._DEVICE_SCHEMAS.get(device_type):
            device_schema = schema.schema

        # Use existing data if no user input
        user_input = user_input or self.config_entry.options

        # Merge base and device-specific schema
        data_schema = self.add_suggested_values_to_schema(
            self._BASE_SCHEMA.extend(device_schema),
            user_input,
        )

        return self.async_show_form(step_id="settings", data_schema=data_schema, errors=errors)
