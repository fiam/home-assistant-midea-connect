"""SmartHome's setup-only BLE proof, account linking and physical confirmation.

Recovered from SmartHome Android 3.19.0; see SMARTHOME_ANDROID_BINDING.md.
Only read operations are polled. A bind/confirmation write is sent once per
explicit attempt. No cloud response text or setup secrets are logged.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .nethome_account import AccountError
from .smarthome_account import (AUTH_CONFIRM, AUTH_GET, BIND, CONFIRM_INFO,
                                LOOKUP)


class SmartHomeBindingClient:
    def __init__(self, account_client, time_zone):
        self._account = account_client
        try:
            ZoneInfo(time_zone)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise AccountError() from None
        self._time_zone = time_zone

    async def prepare(self):
        # Check that this login supplied field-encryption material before any
        # Wi-Fi write. Listing ownership below also exercises native signing.
        self._account.encrypt_session_field("setup")

    async def owned_id(self, serial):
        return await self._account.owned_appliance_id(serial, native=True)

    async def lookup(self, serial, random_code, appliance_type):
        if (not re.fullmatch(r"[A-Za-z0-9]{32}", serial) or len(random_code) != 16
                or appliance_type not in ("AC", "CC")):
            raise AccountError()
        data = await self._account._request(LOOKUP, {
            "sn": self._account.encrypt_session_field(serial),
            "randomCode": random_code.hex(), "forceValidRandomCode": True,
        }, native=True)
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise AccountError()
        matches = []
        for item in data["list"]:
            if not isinstance(item, dict) or not isinstance(item.get("sn"), str):
                raise AccountError()
            decoded = self._account.decrypt_session_field(item["sn"])
            found_serial, _, metadata = decoded.partition("#")
            if found_serial.upper() != serial.upper():
                continue
            model = None
            if metadata:
                if (len(metadata) < 20 or not re.fullmatch(r"[0-9a-fA-F]+", metadata)
                        or metadata[12:14].upper() != appliance_type):
                    raise AccountError()
                model = str(int(metadata[18:20] + metadata[16:18], 16))
            matches.append(model)
        if len(matches) > 1:
            raise AccountError()
        return {"model_number": matches[0]} if matches else None

    async def wait_for_device(self, serial, random_code, appliance_type, timeout=90):
        deadline = asyncio.get_running_loop().time() + timeout
        last_error = AccountError()
        while asyncio.get_running_loop().time() < deadline:
            try:
                result = await self.lookup(serial, random_code, appliance_type)
                if result is not None:
                    return result
            except AccountError as exc:
                last_error = exc
            await asyncio.sleep(3)
        raise last_error

    async def bind(self, serial, name, appliance_type, *, model_number=None):
        if not re.fullmatch(r"[A-Za-z0-9]{32}", serial) or appliance_type not in ("AC", "CC"):
            raise AccountError()
        fields = {"referSn": self._account.encrypt_session_field(serial),
                  "applianceName": name, "applianceType": "0x" + appliance_type,
                  "applianceDes": "", "timeZoneID": self._time_zone}
        if model_number is not None:
            if not re.fullmatch(r"[0-9]{1,5}", model_number):
                raise AccountError()
            fields["modelNumber"] = model_number
        data = await self._account._request(BIND, fields, native=True)
        code = str(data.get("id", "")) if isinstance(data, dict) else ""
        if not re.fullmatch(r"[0-9]{1,20}", code):
            # The write may have succeeded. Caller reconciles exact ownership
            # before deciding whether a later explicit retry can bind again.
            raise AccountError()
        return code

    async def confirmation_status(self, code):
        data = await self._account._request(AUTH_GET, {"applianceCode": code}, native=True)
        status = data.get("status") if isinstance(data, dict) else None
        if type(status) is not int or status not in (0, 1, 2, 3):
            raise AccountError()
        # SuffixConfirmActivity's success callback explicitly accepts 0 or 3.
        return status

    async def start_confirmation(self, code):
        await self._account._request(AUTH_CONFIRM, {"applianceCode": code}, native=True)

    async def confirmation_instructions(self, serial, appliance_type):
        try:
            data = await self._account._request(CONFIRM_INFO, {
                "category": appliance_type, "code": serial[9:17], "version": "",
                "country": "", "sourceSystem": "", "smartProductId": "",
            }, native=True)
        except AccountError:
            return ""
        text = data.get("confirmDesc") if isinstance(data, dict) else None
        if not isinstance(text, str):
            return ""
        # Render manufacturer instructions as bounded plain text, never HTML,
        # Markdown links or interpolated translation syntax.
        text = unescape(re.sub(r"<[^>]*>", " ", text[:4000]))
        text = re.sub(r"[\\`*_{}\[\]<>]", "", text)
        return " ".join(text.split())[:1200]

    def close(self):
        self._account.close()
