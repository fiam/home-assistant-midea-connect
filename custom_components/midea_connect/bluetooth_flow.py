"""Home Assistant's Bluetooth connection and Wi-Fi provisioning screens."""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (SelectSelector,
                                            SelectSelectorConfig, TextSelector,
                                            TextSelectorConfig,
                                            TextSelectorType)

from .account_device import DeviceSetupError, acquire_credentials
from .bluetooth_provision import (ProvisioningState, WifiCredentials,
                                  find_bluetooth_device_on_lan,
                                  provision_device)
from .bluetooth_transport import (BluetoothSetupError, probe_device,
                                  validate_wifi)
from .const import (CONF_ACCOUNT_ENTRY_ID, CONF_ACCOUNT_PROVIDER,
                    CONF_ENTRY_KIND, CONF_KEY, ENTRY_KIND_ACCOUNT,
                    PROVIDER_NETHOME, PROVIDER_SMARTHOME)
from .wifi_credentials import async_get_wifi_store
from .wifi_scan import async_scan_networks

_LOGGER = logging.getLogger(__name__)


class BluetoothFlowMixin:
    def __init__(self):
        super().__init__()
        self._setup_provider = None
        self._provider_return = "bluetooth_setup"
        self._ble_task = None
        self._ble_error = None
        self._ble_error_placeholders = {}
        self._ble_probe_info = None
        self._ble_wifi = None
        self._ble_wifi_to_save = None
        self._ble_ssid = None
        self._ble_wifi_networks = []
        self._ble_device = None
        self._lan_path_chosen = False
        self._ble_lan_host = None
        self._ble_force_setup = False
        self._ble_stage = "cloud_login"
        self._ble_state = ProvisioningState()
        self._ble_state.trace.on_stage = self._stage_changed
        self._bluetooth_discovery = None

    @callback
    def async_remove(self):
        if self._ble_task is not None and not self._ble_task.done():
            self._ble_task.cancel()
        if self._ble_wifi is not None:
            self._ble_wifi.clear()
        if self._ble_wifi_to_save is not None:
            self._ble_wifi_to_save.clear()
            self._ble_wifi_to_save = None
        self._ble_state.clear()
        super().async_remove()

    def _stage_changed(self, stage):
        stages = {"start": "cloud_login", "resume": "cloud_login",
                  "bluetooth_connected": "bluetooth_wifi", "wifi_write_started": "bluetooth_wifi",
                  "cloud_lookup_started": "cloud_link", "ownership_check_started": "cloud_link",
                  "lan_search_started": "lan_search", "lan_found": "cloud_credentials",
                  "local_credentials_verified": "lan_verified"}
        if stage in stages and self._ble_stage != stages[stage]:
            self._ble_stage = stages[stage]
            # Stage notifications must not request another flow GET. A queued
            # refresh can arrive after CREATE_ENTRY has removed this flow and
            # make the frontend report "Invalid flow specified" after success.
            # HA's progress-update event updates the bar without advancing the
            # flow; the progress task owns the single completion transition.
            progress = {"cloud_login": 0.05, "bluetooth_wifi": 0.2,
                        "cloud_link": 0.45, "lan_search": 0.65,
                        "cloud_credentials": 0.8, "lan_verified": 0.95}
            self.async_update_progress(progress[self._ble_stage])

    async def _run_ble_task(self, *, probe=False):
        self._ble_error = None
        self._ble_error_placeholders = {}
        try:
            if probe:
                self._ble_probe_info = None
                # A user selecting a nearby AC expects an immediate BLE
                # connection. LAN discovery is recovery after a failed probe.
                try:
                    self._ble_probe_info = await probe_device(self.hass, self._bluetooth_discovery)
                except BluetoothSetupError:
                    if not self._ble_force_setup and await self._find_existing_lan():
                        return
                    raise
                self._ble_state.serial = self._ble_probe_info.serial
                self._ble_wifi_networks = await async_scan_networks()
            else:
                account = self.hass.config_entries.async_get_entry(
                    self._setup_account_entry_id)
                if account is None or account.data.get(CONF_ENTRY_KIND) != ENTRY_KIND_ACCOUNT:
                    raise BluetoothSetupError("no_setup_accounts")
                if self._ble_lan_host:
                    self._stage_changed("lan_found")
                    self._ble_device = await acquire_credentials(
                        self._get_async_client(), account.data, self._ble_lan_host,
                        expected_serial=self._ble_state.serial)
                else:
                    device = await provision_device(
                        self.hass, self._get_async_client(), self._bluetooth_discovery,
                        account.data, self._ble_state, self._ble_wifi,
                    )
                    if self._ble_wifi_to_save is not None:
                        try:
                            await async_get_wifi_store(self.hass).async_save(self._ble_wifi_to_save)
                        except Exception:
                            # Keep the pending value for Retry, without repeating
                            # Wi-Fi writes or losing the authenticated AC's setup.
                            raise BluetoothSetupError(
                                "ble_wifi_storage_failed") from None
                        self._ble_wifi_to_save.clear()
                        self._ble_wifi_to_save = None
                    self._ble_device = device
        except (BluetoothSetupError, DeviceSetupError) as exc:
            self._ble_error = str(exc)
            if isinstance(exc, BluetoothSetupError) and exc.cloud_code is not None:
                self._ble_error_placeholders = {"code": str(exc.cloud_code)}
        except Exception as exc:
            # Do not include exception messages/tracebacks: libraries may attach
            # request bodies containing passwords or binding codes.
            _LOGGER.error("Bluetooth setup failed (%s)", type(exc).__name__)
            self._ble_error = "ble_setup_incomplete"
        finally:
            if self._ble_wifi is not None:
                self._ble_wifi.clear()
                self._ble_wifi = None
            if not self._ble_state.write_started and self._ble_wifi_to_save is not None:
                self._ble_wifi_to_save.clear()
                self._ble_wifi_to_save = None

    async def _find_existing_lan(self):
        try:
            device = await find_bluetooth_device_on_lan(self._bluetooth_discovery)
        except (OSError, TimeoutError):
            return False
        if device is None:
            return False
        self._ble_lan_host = device.ip
        self._ble_state.serial = device.sn
        return True

    def _setup_accounts(self):
        return {e.entry_id: e for e in self._async_current_entries()
                if e.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT
                and e.data.get(CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME) == self._setup_provider}

    async def async_step_setup_provider(self, user_input=None):
        if user_input is not None:
            self._setup_provider = user_input[CONF_ACCOUNT_PROVIDER]
            return await getattr(self, f"async_step_{self._provider_return}")()
        return self.async_show_form(step_id="setup_provider", data_schema=vol.Schema({
            vol.Required(CONF_ACCOUNT_PROVIDER): vol.In({
                PROVIDER_SMARTHOME: "SmartHome / MSmartHome", PROVIDER_NETHOME: "NetHome Plus"})}))

    async def async_step_setup_account_missing(self, user_input=None):
        if user_input is not None:
            if user_input.get("change_provider"):
                self._setup_provider = None
                return await self.async_step_setup_provider()
            if self._setup_accounts():
                return await getattr(self, f"async_step_{self._provider_return}")()
        return self.async_show_form(step_id="setup_account_missing", data_schema=vol.Schema({
            vol.Optional("change_provider", default=False): bool}), description_placeholders={
                "provider": "SmartHome / MSmartHome" if self._setup_provider == PROVIDER_SMARTHOME else "NetHome Plus"})

    async def async_step_bluetooth_lan_choice(self, user_input=None):
        return self.async_show_menu(step_id="bluetooth_lan_choice", menu_options=[
            "bluetooth_cloud", "restore", "manual", "bluetooth_restart"])

    async def async_step_bluetooth_cloud(self, user_input=None):
        self._lan_path_chosen = True
        return await self.async_step_bluetooth_lan()

    async def async_step_bluetooth_restart(self, user_input=None):
        return await self._restart_bluetooth_pairing()

    async def async_step_bluetooth_lan(self, user_input=None):
        if not self._lan_path_chosen:
            return await self.async_step_bluetooth_lan_choice()

        if self._setup_provider is None:
            self._provider_return = "bluetooth_lan"
            return await self.async_step_setup_provider()
        accounts = self._setup_accounts()
        if not accounts:
            return await self.async_step_setup_account_missing()
        if user_input is not None:
            if user_input.get("setup_wifi"):
                if user_input.get(CONF_ACCOUNT_ENTRY_ID) in accounts:
                    self._setup_account_entry_id = user_input[CONF_ACCOUNT_ENTRY_ID]
                self._ble_force_setup = True
                self._ble_lan_host = None
                self._ble_error = None
                return await self.async_step_bluetooth_connect()
            if user_input[CONF_ACCOUNT_ENTRY_ID] in accounts:
                self._setup_account_entry_id = user_input[CONF_ACCOUNT_ENTRY_ID]
                return await self.async_step_bluetooth_provision()
        return self.async_show_form(
            step_id="bluetooth_lan", errors={"base": self._ble_error} if self._ble_error else {},
            description_placeholders={
                "name": self._bluetooth_discovery.name, "host": self._ble_lan_host},
            data_schema=vol.Schema({
                vol.Required(CONF_ACCOUNT_ENTRY_ID, default=self._ble_account_default(accounts)): vol.In({
                    key: self._ble_account_label(entry) for key, entry in accounts.items()}),
                vol.Optional("setup_wifi", default=False): cv.boolean,
            }))

    async def async_step_bluetooth_connect(self, user_input=None):
        if self._bluetooth_discovery is None:
            return self.async_abort(reason="no_bluetooth_devices")
        if self._ble_task is None:
            self._ble_task = self.hass.async_create_task(
                self._run_ble_task(probe=True), eager_start=False)
        if not self._ble_task.done():
            return self.async_show_progress(
                step_id="bluetooth_connect", progress_action="bluetooth_connecting",
                progress_task=self._ble_task,
            )
        self._ble_task = None
        return self.async_show_progress_done(next_step_id="bluetooth_setup")

    async def async_step_bluetooth_connect_retry(self, user_input=None):
        return await self.async_step_bluetooth_connect()

    async def async_step_bluetooth_setup(self, user_input=None):
        if self._bluetooth_discovery is None:
            return self.async_abort(reason="no_bluetooth_devices")
        if self._ble_lan_host:
            return await self.async_step_bluetooth_lan(user_input)
        if self._ble_probe_info is None:
            return self.async_show_form(step_id="bluetooth_connect_retry", data_schema=vol.Schema({}),
                                        errors={"base": self._ble_error or "ble_cannot_connect"})
        if self._setup_provider is None:
            self._provider_return = "bluetooth_setup"
            return await self.async_step_setup_provider()
        accounts = self._setup_accounts()
        if not accounts:
            return await self.async_step_setup_account_missing()
        wifi_store = async_get_wifi_store(self.hass)
        saved_networks = await wifi_store.async_networks()
        errors = {"base": self._ble_error} if self._ble_error else {}
        if user_input is not None:
            errors = {}
            self._ble_ssid = user_input["ssid"]
            wifi = WifiCredentials(
                self._ble_ssid, user_input.get("wifi_password", ""))
            mode = user_input.get("password_mode", "open" if user_input.get("open_network") else
                                  "saved" if not wifi.password and user_input.get("use_saved_password", True) else "new")
            if mode == "open":
                if wifi.password:
                    errors["base"] = "ble_open_network_password"
            elif mode == "saved":
                saved = await wifi_store.async_get(wifi.ssid)
                if saved is None:
                    errors["base"] = "ble_wifi_password_required"
                else:
                    wifi.clear()
                    wifi = saved
            elif not wifi.password:
                errors["base"] = "ble_wifi_password_required"
            try:
                validate_wifi(wifi.ssid, wifi.password)
            except BluetoothSetupError as exc:
                errors["base"] = str(exc)
            if user_input[CONF_ACCOUNT_ENTRY_ID] not in accounts:
                errors["base"] = "no_setup_accounts"
            if not errors:
                self._setup_account_entry_id = user_input[CONF_ACCOUNT_ENTRY_ID]
                self._ble_wifi = wifi
                if self._ble_wifi_to_save is not None:
                    self._ble_wifi_to_save.clear()
                self._ble_wifi_to_save = WifiCredentials(
                    wifi.ssid, wifi.password)
                return await self.async_step_bluetooth_provision()
            wifi.clear()
        ssid_default = self._ble_ssid or (
            saved_networks[0] if saved_networks else vol.UNDEFINED)
        network_options = list(dict.fromkeys(
            saved_networks + self._ble_wifi_networks))
        schema = {
            vol.Required(CONF_ACCOUNT_ENTRY_ID, default=self._ble_account_default(accounts)): vol.In({
                key: self._ble_account_label(entry) for key, entry in accounts.items()}),
            vol.Required("ssid", default=ssid_default): SelectSelector(SelectSelectorConfig(
                options=network_options, custom_value=True, mode="dropdown")),
            vol.Optional("wifi_password"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required("password_mode", default="new"): vol.In(
                {"new": "Enter password", "open": "Open network"} |
                ({"saved": "Use saved password for this SSID"} if saved_networks else {})),
        }
        return self.async_show_form(step_id="bluetooth_setup", data_schema=vol.Schema(schema), errors=errors,
                                    description_placeholders={"name": self._bluetooth_discovery.name})

    async def async_step_bluetooth_provision(self, user_input=None):
        if self._bluetooth_discovery is None or not hasattr(self, "_setup_account_entry_id"):
            return self.async_abort(reason="no_bluetooth_devices")
        if self._ble_task is None:
            self._ble_task = self.hass.async_create_task(
                self._run_ble_task(), eager_start=False)
        if not self._ble_task.done():
            return self.async_show_progress(
                step_id="bluetooth_provision", progress_action="bluetooth_provisioning",
                progress_task=self._ble_task,
            )
        self._ble_task = None
        return self.async_show_progress_done(next_step_id="bluetooth_finish")

    async def async_step_bluetooth_finish(self, user_input=None):
        if self._ble_device is not None:
            device = self._ble_device
            await self.async_set_unique_id(str(device.id))
            existing = next((e for e in self._async_current_entries()
                            if e.unique_id == str(device.id)), None)
            if existing is not None:
                return self.async_update_reload_and_abort(existing, data_updates={
                    CONF_HOST: device.ip, CONF_PORT: device.port, CONF_TOKEN: device.token,
                    CONF_KEY: device.key, CONF_ACCOUNT_ENTRY_ID: self._setup_account_entry_id,
                    "bluetooth_address": self._bluetooth_discovery.address, "sn": self._ble_state.serial,
                    "cloud_appliance_id": self._ble_state.appliance_code,
                }, reason="account_tokens_updated")
            return await self._create_entry_from_device(device)
        if self._ble_lan_host:
            return await self.async_step_bluetooth_lan(user_input)
        if not self._ble_state.write_started:
            return await self.async_step_bluetooth_setup(user_input=None)
        if self._ble_error == "smarthome_confirmation_required":
            return await self.async_step_bluetooth_confirm(user_input)
        if self._ble_error == "smarthome_pairing_expired":
            return await self.async_step_bluetooth_pairing_expired(user_input)
        if user_input is not None:
            if user_input.get("change_wifi") and not self._ble_state.bound:
                return await self._restart_bluetooth_pairing()
            return await self.async_step_bluetooth_provision()
        schema = {} if self._ble_state.bound else {
            vol.Optional("change_wifi", default=False): cv.boolean}
        return self.async_show_form(step_id="bluetooth_finish", data_schema=vol.Schema(schema),
                                    errors={
                                        "base": self._ble_error or "ble_setup_incomplete"},
                                    description_placeholders={"name": self._bluetooth_discovery.name,
                                                              **self._ble_error_placeholders})

    async def _restart_bluetooth_pairing(self):
        self._ble_state.clear()
        self._ble_state = ProvisioningState(serial=self._ble_state.serial)
        self._ble_state.trace.on_stage = self._stage_changed
        self._ble_probe_info = None
        self._ble_force_setup = True
        self._ble_lan_host = None
        self._ble_error = None
        self._ble_error_placeholders = {}
        return await self.async_step_bluetooth_connect()

    async def async_step_bluetooth_pairing_expired(self, user_input=None):
        if user_input is not None:
            return await self._restart_bluetooth_pairing()
        return self.async_show_form(
            step_id="bluetooth_pairing_expired", data_schema=vol.Schema({}),
            description_placeholders={"name": self._bluetooth_discovery.name},
        )

    async def async_step_bluetooth_confirm(self, user_input=None):
        if user_input is not None:
            if user_input.get("restart_confirmation"):
                self._ble_state.confirmation_started = False
            return await self.async_step_bluetooth_provision()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {vol.Optional("restart_confirmation", default=False): cv.boolean}),
            description_placeholders={"name": self._bluetooth_discovery.name,
                                      "instructions": self._ble_state.confirmation_instructions},
        )

    def _ble_account_default(self, accounts):
        selected = getattr(self, "_setup_account_entry_id", None)
        return selected if selected in accounts else next(iter(accounts))

    @staticmethod
    def _ble_account_label(entry):
        provider = ("SmartHome" if entry.data.get(CONF_ACCOUNT_PROVIDER) == PROVIDER_SMARTHOME
                    else "NetHome Plus")
        return f"{provider}: {entry.data.get(CONF_EMAIL, entry.title)}"
