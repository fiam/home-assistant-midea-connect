"""SmartHome email-code signup and existing-account setup screens."""
from __future__ import annotations

import re
from time import monotonic

import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import httpx_client
from homeassistant.helpers.selector import (TextSelector, TextSelectorConfig,
                                            TextSelectorType)

from .const import PROVIDER_SMARTHOME
from .nethome_account import AccountError, RegistrationUncertain, valid_email
from .smarthome_account import (PRIVACY_URL, TERMS_URL, SmartHomeAccountClient,
                                valid_smarthome_password)


def _text(kind):
    return TextSelector(TextSelectorConfig(type=kind))


class SmartHomeFlowMixin:
    """Verify the email before sending a single registration request."""

    _smarthome_client = None
    _smarthome_regions = None
    _smarthome_random_code = None
    _smarthome_code_sent = False
    _smarthome_send_uncertain = False
    _smarthome_next_send = 0
    _smarthome_submitted = False

    @callback
    def async_remove(self):
        self._smarthome_client = None
        self._smarthome_random_code = None
        self._smarthome_regions = None
        super().async_remove()

    def _smarthome(self):
        if self._smarthome_client is None:
            self._smarthome_client = SmartHomeAccountClient(
                httpx_client.get_async_client(self.hass))
        return self._smarthome_client

    async def async_step_smarthome_account(self, user_input=None):
        self._account_provider = PROVIDER_SMARTHOME
        return self.async_show_menu(step_id="smarthome_account", menu_options=[
            "smarthome_register", "smarthome_login"],
            description_placeholders={"accounts": self._saved_account_names(PROVIDER_SMARTHOME)})

    async def async_step_smarthome_register(self, user_input=None):
        self._account_provider = PROVIDER_SMARTHOME
        if self._smarthome_submitted:
            return await self.async_step_smarthome_finish()
        if self._smarthome_code_sent:
            return await self.async_step_smarthome_verify()
        errors = {}
        if self._smarthome_regions is None:
            try:
                self._smarthome_regions = await self._smarthome().regions()
            except AccountError:
                return self.async_show_form(step_id="smarthome_prepare", data_schema=vol.Schema({}),
                                            errors={"base": "account_connection_failed"})
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            country = user_input["region_code"]
            if not valid_email(email):
                errors[CONF_EMAIL] = "account_invalid_email"
            if country not in self._smarthome_regions:
                errors["region_code"] = "account_invalid_region"
            if not user_input["accept_terms"]:
                errors["accept_terms"] = "account_accept_terms"
            if monotonic() < self._smarthome_next_send:
                errors["base"] = "smarthome_resend_wait"
            if not errors:
                await self._account_unique_id(email)
                try:
                    await self._smarthome().prepare_registration(email, country)
                except AccountError as exc:
                    errors["base"] = ("account_already_exists" if exc.code == 3124
                                      else "account_connection_failed")
                else:
                    self._account_email, self._account_region = email, country
                    try:
                        await self._send_smarthome_code()
                    except AccountError:
                        errors["base"] = "smarthome_code_send_failed"
                    else:
                        return await self.async_step_smarthome_verify()
        return self.async_show_form(
            step_id="smarthome_register", errors=errors,
            description_placeholders={
                "terms_url": TERMS_URL, "privacy_url": PRIVACY_URL},
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): _text(TextSelectorType.EMAIL),
                vol.Required("region_code"): vol.In(self._smarthome_regions),
                vol.Required("accept_terms", default=False): bool,
            }))

    async def async_step_smarthome_prepare(self, user_input=None):
        return await self.async_step_smarthome_register()

    async def _send_smarthome_code(self):
        self._smarthome_next_send = monotonic() + 60
        self._smarthome_send_uncertain = False
        try:
            await self._smarthome().send_code(self._account_email)
        except RegistrationUncertain:
            self._smarthome_send_uncertain = True
        self._smarthome_code_sent = True

    async def async_step_smarthome_verify(self, user_input=None):
        if self._smarthome_submitted:
            return await self.async_step_smarthome_finish()
        if not self._smarthome_code_sent or not self._account_email:
            return self.async_abort(reason="account_setup_expired")
        if self._smarthome_random_code:
            return await self.async_step_smarthome_password()
        errors = {}
        if user_input is not None:
            if user_input.get("resend_code"):
                if monotonic() < self._smarthome_next_send:
                    errors["base"] = "smarthome_resend_wait"
                else:
                    try:
                        await self._send_smarthome_code()
                    except AccountError:
                        errors["base"] = "smarthome_code_send_failed"
            else:
                code = user_input.get("verification_code", "").strip()
                if not re.fullmatch(r"[0-9]{6}", code):
                    errors["verification_code"] = "smarthome_invalid_code"
                else:
                    try:
                        self._smarthome_random_code = await self._smarthome().verify_code(self._account_email, code)
                    except AccountError as exc:
                        errors["base"] = "smarthome_invalid_code" if exc.code is not None else "account_connection_failed"
                    else:
                        return await self.async_step_smarthome_password()
        if self._smarthome_send_uncertain and not errors:
            errors["base"] = "smarthome_code_send_uncertain"
        return self.async_show_form(step_id="smarthome_verify", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Optional("verification_code"): str,
                                        vol.Optional("resend_code", default=False): bool,
                                    }))

    async def async_step_smarthome_password(self, user_input=None):
        if self._smarthome_submitted:
            return await self.async_step_smarthome_finish()
        if not self._account_email or not self._smarthome_random_code:
            return self.async_abort(reason="account_setup_expired")
        errors = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            if not valid_smarthome_password(password):
                errors[CONF_PASSWORD] = "smarthome_invalid_password"
            if password != user_input["confirm_password"]:
                errors["confirm_password"] = "account_password_mismatch"
            if not errors:
                # From this point retries verify login; they never register again.
                self._smarthome_submitted = True
                try:
                    await self._smarthome().register(
                        self._account_email, password, self._account_region, self._smarthome_random_code)
                except RegistrationUncertain:
                    self._account_uncertain = True
                    return await self.async_step_smarthome_finish()
                except AccountError as exc:
                    if exc.code == 3124:
                        return await self.async_step_smarthome_finish()
                    # A definite server rejection permits editing/retrying.
                    self._smarthome_submitted = False
                    errors["base"] = "account_registration_failed"
                else:
                    self._smarthome_random_code = None
                    return await self.async_step_smarthome_finish({CONF_PASSWORD: password})
        return self.async_show_form(step_id="smarthome_password", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_PASSWORD): _text(TextSelectorType.PASSWORD),
                                        vol.Required("confirm_password"): _text(TextSelectorType.PASSWORD),
                                    }))

    async def async_step_smarthome_finish(self, user_input=None):
        if not self._smarthome_submitted or not self._account_email:
            return self.async_abort(reason="account_setup_expired")
        self._smarthome_random_code = None
        errors = {}
        if user_input is not None:
            try:
                await self._smarthome().check_login(self._account_email, user_input[CONF_PASSWORD])
            except AccountError:
                errors["base"] = "smarthome_finish_login_failed"
            else:
                return await self._save_account(self._account_email, user_input[CONF_PASSWORD])
        if self._account_uncertain and not errors:
            errors["base"] = "smarthome_registration_uncertain"
        return self.async_show_form(step_id="smarthome_finish", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_PASSWORD): _text(TextSelectorType.PASSWORD),
                                    }))

    async def async_step_smarthome_login(self, user_input=None):
        self._account_provider = PROVIDER_SMARTHOME
        errors = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            if not valid_email(email):
                errors[CONF_EMAIL] = "account_invalid_email"
            else:
                await self._account_unique_id(email)
                try:
                    await self._smarthome().check_login(email, user_input[CONF_PASSWORD])
                except AccountError as exc:
                    errors["base"] = ("smarthome_account_not_found" if exc.code in (3102, 10004)
                                      else "account_login_failed" if exc.code is not None
                                      else "account_connection_failed")
                else:
                    return await self._save_account(email, user_input[CONF_PASSWORD])
        return self.async_show_form(step_id="smarthome_login", errors=errors,
                                    data_schema=vol.Schema({
                                        vol.Required(CONF_EMAIL): _text(TextSelectorType.EMAIL),
                                        vol.Required(CONF_PASSWORD): _text(TextSelectorType.PASSWORD),
                                    }))
