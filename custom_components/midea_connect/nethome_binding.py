"""Setup-only OEM device lookup and binding recovered from NetHome Plus.

OemServerApiHelper uses business signatures 2.0; the OVERSEAS_OEM SDK uses
signature 2.1 and encrypts serial numbers with a per-session AES key.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import time
from hashlib import sha256
from secrets import token_bytes
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .nethome_account import APP_ID, APP_KEY, AccountError

CLIENT_ID = "13aaf6e6e1bf8848b615ed872b91151a"
ACCESS_PATH = "/midea/open/business/v1/token"
LOOKUP_PATH = "/v2/open/sdk/device/sn/apExists"
BIND_PATH = "/midea/open/business/v1/bind"
_LOGGER = logging.getLogger(__name__)


class NetHomeBindingClient:
    """Bind only the serial and one-time random code obtained during this setup."""

    def __init__(self, http_client, account_client):
        self._http = http_client
        self._account = account_client
        self._base = "https://eu.dollin.net" if "-eu." in account_client.region_domain else "https://us.dollin.net"
        self._token = None

    async def _request(self, path, fields, *, sdk=False):
        if path not in (ACCESS_PATH, LOOKUP_PATH, BIND_PATH):
            raise ValueError("Unsupported binding endpoint")
        stamp = int(time.time() * 1000)
        body = {"reqId": str(uuid4()), "stamp": str(stamp)
                if sdk else stamp, **fields}
        payload = json.dumps(body, sort_keys=True, separators=(
            ",", ":"), ensure_ascii=False).encode()
        token = self._token or ""
        key = sha256(token.encode()).digest()[:16] if sdk else APP_KEY.encode()
        signature = base64.b64encode(
            hmac.new(key, b"POST" + path.encode() + payload, sha256).digest()).decode()
        headers = {"Content-Type": "application/json", "clientType": "1", "appVersion": "5.44.0",
                   "clientId": CLIENT_ID, "userSrc": "2", "language": "en_US", "appId": "7010",
                   "SignatureVersion": "2.1" if sdk else "2.0", "Signature": signature,
                   "Authorization": "Bearer " + token if sdk else token,
                   "accessToken": "Bearer " + token if sdk else token}
        if sdk:
            headers.update(platform="0", systemVersion="14")
        try:
            response = await self._http.post(self._base + path, content=payload, headers=headers,
                                             timeout=15, follow_redirects=False)
            response.raise_for_status()
            result = response.json()
            code = int(result["code"])
            if code:
                if path == BIND_PATH:
                    _LOGGER.warning(
                        "Midea account linking rejected (code %s)", code)
                raise AccountError(code)
            if path == BIND_PATH:
                # OemServerApiHelper.bindDevice checks only code == 0. Its
                # data field is optional and need not contain JSON.
                return None
            data = result.get("data")
            if isinstance(data, str):
                data = json.loads(data)
            return data
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            if path == BIND_PATH:
                # Never log a response body, request, or exception message.
                response = getattr(exc, "response", None)
                _LOGGER.warning("Midea account linking response unavailable (%s, HTTP %s)",
                                type(exc).__name__, getattr(response, "status_code", None))
            raise AccountError() from None

    async def prepare(self):
        user_id = self._account.user_id
        if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user_id):
            raise AccountError()
        data = await self._request(ACCESS_PATH, {"openUserId": user_id})
        token = data.get("accessToken") or data.get(
            "access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AccountError()
        self._token = token

    async def lookup(self, serial, random_code):
        if not self._token or not re.fullmatch(r"[A-Za-z0-9]{32}", serial) or len(random_code) != 16:
            raise AccountError()
        key = sha256(self._token.encode()).digest()[:16]
        iv = token_bytes(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(serial.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted_sn = (iv + cipher.update(padded) + cipher.finalize()).hex()
        data = await self._request(LOOKUP_PATH, {
            "clientId": CLIENT_ID, "sn": encrypted_sn,
            "forceValidRandomCode": True, "randomCode": random_code.hex(),
        }, sdk=True)
        if not isinstance(data, dict) or not isinstance(data.get("applianceList"), list):
            raise AccountError()
        devices = data["applianceList"]
        if not devices:
            return None
        # Do not silently pick another appliance from an ambiguous response.
        if len(devices) != 1 or not isinstance(devices[0], dict):
            raise AccountError()
        appliance = devices[0]
        code, verification = appliance.get(
            "applianceCode"), appliance.get("verificationCode")
        if (not isinstance(code, (str, int)) or isinstance(code, bool)
                or not re.fullmatch(r"[0-9]{1,20}", str(code))
                or not isinstance(verification, str) or not verification):
            raise AccountError()
        return str(code), verification

    async def wait_for_device(self, serial, random_code, timeout=90):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if result := await self.lookup(serial, random_code):
                    return result
            except AccountError:
                pass  # Read-only lookup is polled while the module logs in.
            await asyncio.sleep(3)
        raise AccountError()

    async def bind(self, appliance_code, verification_code, name, *, appliance_type="AC"):
        if not self._token or appliance_type not in ("AC", "CC"):
            raise AccountError()
        await self._request(BIND_PATH, {
            "applianceCode": appliance_code, "verificationCode": verification_code,
            # MSDevice.setDeviceType prefixes the advertised category with 0x
            # before ConfigManger passes it to OemServerApiHelper.bindDevice.
            "oldAppId": APP_ID, "applianceName": name, "applianceType": "0x" + appliance_type,
        })

    def close(self):
        self._token = None
