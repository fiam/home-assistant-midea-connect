"""Email account creation and activation, separate from AC config entries."""
from __future__ import annotations

from hashlib import sha256

import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import httpx_client
from homeassistant.helpers.selector import (TextSelector, TextSelectorConfig,
                                            TextSelectorType)

from .const import (CONF_ACCOUNT_PROVIDER, CONF_ENTRY_KIND, ENTRY_KIND_ACCOUNT,
                    PROVIDER_NETHOME, PROVIDER_SMARTHOME)
from .nethome_account import (PRIVACY_URL, TERMS_URL, AccountError,
                              NetHomeAccountClient, RegistrationUncertain,
                              valid_email, valid_password)
from .smarthome_account import SmartHomeAccountClient
from .smarthome_flow import SmartHomeFlowMixin


class AccountFlowMixin(SmartHomeFlowMixin):
    """Persist a setup account separately from each AC's local credentials."""

    _account_regions = None
    _account_email = None
    _account_submitted = False
    _account_uncertain = False
    _account_region = None
    _account_provider = PROVIDER_NETHOME

    @callback
    def async_remove(self):
        """Drop transient account information when the flow ends or is canceled."""
        self._account_email = None
        self._account_regions = None
        super().async_remove()

    def _account_client(self):
        if self._account_provider == PROVIDER_SMARTHOME:
            return SmartHomeAccountClient(httpx_client.get_async_client(self.hass))
        return NetHomeAccountClient(httpx_client.get_async_client(self.hass))

    async def async_step_account(self, user_input=None):
        return self.async_show_menu(step_id="account", menu_options=[
            "smarthome_account", "nethome_account"])

    async def async_step_nethome_account(self, user_input=None):
        self._account_provider = PROVIDER_NETHOME
        return self.async_show_menu(step_id="nethome_account", menu_options=[
            "account_register", "account_login"],
            description_placeholders={"accounts": self._saved_account_names(PROVIDER_NETHOME)})

    def _saved_account_names(self, provider):
        entries = [e.data.get(CONF_EMAIL, e.title) for e in self._async_current_entries()
                   if e.data.get(CONF_ENTRY_KIND) == ENTRY_KIND_ACCOUNT
                   and e.data.get(CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME) == provider]
        return "\n".join(f"- {email}" for email in entries) or "No saved accounts."

    async def _account_unique_id(self, email):
        digest = sha256(email.casefold().encode()).hexdigest()
        await self.async_set_unique_id(self._account_provider + "-account-" + digest)
        self._abort_if_unique_id_configured()

    async def _save_account(self, email, password):
        await self._account_unique_id(email)
        data = {CONF_ENTRY_KIND: ENTRY_KIND_ACCOUNT,
                CONF_EMAIL: email, CONF_PASSWORD: password,
                CONF_ACCOUNT_PROVIDER: self._account_provider}
        if self._account_region:
            data["region_code"] = self._account_region
        if self._account_provider == PROVIDER_SMARTHOME:
            data[CONF_ACCOUNT_PROVIDER] = PROVIDER_SMARTHOME
            return self.async_create_entry(title=f"SmartHome — {email}", data=data)
        return self.async_create_entry(title=f"NetHome Plus — {email}", data=data)

    async def async_step_account_register(self, user_input=None):
        """Collect account details and require explicit submission to register."""
        self._account_provider = PROVIDER_NETHOME
        if self._account_submitted:
            return await self.async_step_account_activate()
        errors = {}
        if self._account_regions is None:
            try:
                self._account_regions = await self._account_client().regions()
            except AccountError:
                return self.async_show_form(
                    step_id="account_prepare", data_schema=vol.Schema({}),
                    errors={"base": "account_connection_failed"})
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            if not valid_email(email):
                errors[CONF_EMAIL] = "account_invalid_email"
            if not valid_password(password):
                errors[CONF_PASSWORD] = "account_invalid_password"
            if password != user_input["confirm_password"]:
                errors["confirm_password"] = "account_password_mismatch"
            if not user_input["accept_terms"]:
                errors["accept_terms"] = "account_accept_terms"
            if user_input["region_code"] not in self._account_regions:
                errors["region_code"] = "account_invalid_region"
            if not errors:
                await self._account_unique_id(email)
                try:
                    await self._account_client().register(
                        email, password, user_input["region_code"])
                except RegistrationUncertain:
                    # Do not repeat a mutation after a lost/invalid response.
                    self._account_uncertain = True
                except AccountError as exc:
                    errors["base"] = ("account_already_exists" if exc.code == 3124
                                      else "account_registration_failed")
                if not errors:
                    self._account_email = email
                    self._account_region = user_input["region_code"]
                    self._account_submitted = True
                    return await self.async_step_account_activate()
        # Never return password defaults/suggested values to the frontend.
        return self.async_show_form(
            step_id="account_register", errors=errors,
            description_placeholders={
                "terms_url": TERMS_URL, "privacy_url": PRIVACY_URL},
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required("confirm_password"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required("region_code"): vol.In(self._account_regions),
                vol.Required("accept_terms", default=False): bool,
            }))

    async def async_step_account_prepare(self, user_input=None):
        """Retry loading the region list; never submit a registration here."""
        return await self.async_step_account_register()

    async def async_step_account_activate(self, user_input=None):
        """Let the user follow their email link, then verify an activated login."""
        if not self._account_submitted or not self._account_email:
            return self.async_abort(reason="account_setup_expired")
        errors = {}
        if user_input is not None:
            try:
                await self._account_client().check_login(
                    self._account_email, user_input[CONF_PASSWORD])
            except AccountError as exc:
                if exc.code in (3103, 3202):
                    errors["base"] = "account_not_activated"
                elif exc.code in (3101, 3102):
                    errors["base"] = "account_login_failed"
                else:
                    errors["base"] = "account_connection_failed"
            else:
                return await self._save_account(self._account_email, user_input[CONF_PASSWORD])
        if self._account_uncertain and not errors:
            errors["base"] = "account_registration_uncertain"
        return self.async_show_form(
            step_id="account_activate", errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }))

    async def async_step_account_login(self, user_input=None):
        """Save an existing activated email account for device setup."""
        self._account_provider = PROVIDER_NETHOME
        errors = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self._account_unique_id(email)
            try:
                await self._account_client().check_login(email, user_input[CONF_PASSWORD])
            except AccountError as exc:
                errors["base"] = ("account_not_activated" if exc.code in (3103, 3202)
                                  else "account_login_failed")
            else:
                return await self._save_account(email, user_input[CONF_PASSWORD])
        return self.async_show_form(step_id="account_login", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                                        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                                    }))

    async def async_step_account_password(self, user_input=None):
        """Replace a saved setup password after verifying the account login."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_ENTRY_KIND) != ENTRY_KIND_ACCOUNT:
            return self.async_abort(reason="account_setup_expired")
        self._account_provider = entry.data.get(
            CONF_ACCOUNT_PROVIDER, PROVIDER_NETHOME)
        errors = {}
        if user_input is not None:
            try:
                await self._account_client().check_login(
                    entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            except AccountError:
                errors["base"] = "account_login_failed"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(step_id="account_password", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                                    }))
