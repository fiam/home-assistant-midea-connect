"""Setup-only SmartHome account API, traced from its public signup application.

Endpoints and signing were recovered from the SmartHome app. No registration/code request is
retried automatically. Cloud sessions live only for the current setup operation.
"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime
from hashlib import md5, sha256
from secrets import randbelow, token_hex
from time import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from .nethome_account import AccountError, RegistrationUncertain, valid_email

APP_KEY = "ac21b9f9cbfe4ca5a88562ef25e2b768"
BASE_URL = "https://mp-prod.appsmb.com/mas/v5/app/proxy"
TERMS_URL = "https://qrscan.aiiciot.com/AboutApp/userAgreement/licenseAndAgreement_en.html"
PRIVACY_URL = "https://qrscan.aiiciot.com/AboutApp/lawPolicy/lawPolicy_en.html"
REGIONS = "/v1/support/areas/retrive"
COUNTRY_ROUTE = "/v1/unitcenter/router/country"
USER_ROUTE = "/v1/unitcenter/router/user/name"
SEND_CODE = "/v1/user/email/verify/code/get"
VERIFY_CODE = "/v1/user/email/verify/code/auth"
REGISTER = "/mj/user/register"
LOGIN_ID = "/v1/user/login/id/get/new"
LOGIN = "/mj/user/login"
APPLIANCES = "/v1/appliance/user/list/get"
TOKEN = "/v1/iot/secure/getToken"
LOOKUP = "/v1/appliance/sn/apExists"
BIND = "/v1/appliance/user/bind"
AUTH_GET = "/v1/appliance/auth/get"
AUTH_CONFIRM = "/v1/appliance/auth/confirm"
CONFIRM_INFO = "api-product/app/getIotConfirminfo"
_SETUP_PATHS = {LOOKUP, BIND, AUTH_GET, AUTH_CONFIRM, CONFIRM_INFO}
_PATHS = {REGIONS, COUNTRY_ROUTE, USER_ROUTE, SEND_CODE, VERIFY_CODE,
          REGISTER, LOGIN_ID, LOGIN, APPLIANCES, TOKEN} | _SETUP_PATHS


def valid_smarthome_password(value: str) -> bool:
    """SmartHome's signup form requires 8–20 characters, letters and digits."""
    return (8 <= len(value) <= 20 and " " not in value
            and re.search(r"[A-Za-z]", value) is not None
            and re.search(r"[0-9]", value) is not None)


def _app_material():
    digest = sha256(APP_KEY.encode()).hexdigest()
    return digest[:16].encode(), digest[16:32].encode()


def encrypt_field(value: str) -> str:
    """The official web client encrypts account fields with its public app key."""
    if not value:
        return ""
    key, iv = _app_material()
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(value.encode(), 16)).hex()


def _decrypt(value, key, iv):
    return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(bytes.fromhex(value)), 16)


class SmartHomeAccountClient:
    """Use the official web client's account flow and region routing."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._base_url = BASE_URL
        self._device_id = str(uuid4())
        self._access_token = ""
        self._session_key = self._session_iv = None
        self._uid = ""

    def _iot_fields(self):
        return {"format": "2", "appId": "1010", "clientType": 8,
                "clientSrc": 8, "stamp": datetime.now().astimezone().strftime("%Y%m%d%H%M%S"),
                "reqId": str(uuid4()), "language": "en_US", "deviceId": self._device_id}

    async def _request(self, path, fields, *, native=False):
        if path not in _PATHS:
            raise ValueError("Unsupported SmartHome setup endpoint")
        if path in _SETUP_PATHS and (not native or not self._access_token):
            raise AccountError()
        body = {**self._iot_fields(), **fields}
        if native:
            # MasIotData defaults and the overseas signature used by the
            # pinned msmart-ng client. Account creation keeps its web transport.
            body.pop("clientSrc", None)
            body.update(clientType=1, src="1010", format=2, reqId=uuid4().hex,
                        uid=self._uid, homegroupId="", appVersion="3.19.0",
                        appVNum="3.19.0", clientVersion="3.19.0")
        if path in (REGISTER, LOGIN):
            body = {"timestamp": int(time() * 1000), "data": {
                "appKey": APP_KEY, "deviceId": self._device_id,
                "deviceName": "Home Assistant", "platform": 10,
                "loginType": 1, "clientData": {}}, "iotData": body}
        encoded = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        # The web application signs with MD5; the Android client's HMAC scheme
        # is a different transport. Registration explicitly omits this header.
        if native:
            random = token_hex(16)
            headers.update(random=random, secretVersion="1", sign=hmac.new(
                b"PROD_VnoClJI9aikS8dyy", ("meicloud" +
                                           encoded + random).encode(),
                sha256).hexdigest())
        elif path != REGISTER:
            random = str(randbelow(90000) + 10000)
            headers.update(random=random, sign=md5(
                ("meicloud" + encoded + random).encode()).hexdigest())
        if self._access_token:
            headers["accessToken"] = self._access_token
        try:
            response = await self._client.post(
                self._base_url, params={"alias": path}, content=encoded,
                headers=headers, timeout=15, follow_redirects=False)
            response.raise_for_status()
            result = response.json()
            code = int(result["code"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            error = RegistrationUncertain if path in (
                REGISTER, SEND_CODE) else AccountError
            raise error() from None
        if code not in ((0, 200) if native else (0,)):
            raise AccountError(code)
        return result.get("data")

    def _set_route(self, data):
        try:
            url = urlsplit(data["masUrl"])
            if (url.scheme != "https" or url.username or url.password
                    or not re.fullmatch(r"mp(?:-[a-z]{2,3})?-prod\.appsmb\.com", url.netloc)
                    or url.path != "/mas/v5/app/proxy"
                    or url.query not in ("", "alias=") or url.fragment):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AccountError() from None
        self._base_url = f"https://{url.netloc}{url.path}"

    async def regions(self):
        result = await self._request(REGIONS, {"returnPhoneAreaCode": 1, "src": "10"})
        try:
            regions = {item["countryCode"]: item["area"] for item in result["list"]
                       if re.fullmatch(r"[A-Z]{2}", item["countryCode"])
                       and isinstance(item["area"], str)}
            if not regions:
                raise ValueError
            return regions
        except (KeyError, TypeError, ValueError):
            raise AccountError() from None

    async def prepare_registration(self, email, country):
        """Check existence, then route a new account to its selected country."""
        if not valid_email(email) or not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("Invalid account fields")
        self._base_url = BASE_URL
        try:
            await self._request(USER_ROUTE, {
                "userName": encrypt_field(email), "userType": "0", "platformId": "1"})
        except AccountError as exc:
            if exc.code != 10004:
                raise
        else:
            raise AccountError(3124)
        self._set_route(await self._request(COUNTRY_ROUTE, {
            "platformId": "1", "countryCode": country}))

    async def send_code(self, email):
        if not valid_email(email):
            raise ValueError("Invalid email")
        await self._request(SEND_CODE, {"type": "4", "email": encrypt_field(email)})

    async def verify_code(self, email, code):
        if not valid_email(email) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("Invalid verification fields")
        data = await self._request(VERIFY_CODE, {
            "verifyId": code, "newEmail": encrypt_field(email)})
        if not isinstance(data, dict) or not isinstance(data.get("randomCode"), str) or not data["randomCode"]:
            raise AccountError()
        return data["randomCode"]

    async def register(self, email, password, country, random_code):
        if (not valid_email(email) or not valid_smarthome_password(password)
                or not re.fullmatch(r"[A-Z]{2}", country) or not random_code):
            raise ValueError("Invalid account fields")
        await self._request(REGISTER, {
            "email": encrypt_field(email), "mobile": "",
            "password": encrypt_field(sha256(password.encode()).hexdigest()),
            "randomCode": random_code, "countryCode": country})

    async def check_login(self, email, password):
        self._base_url = BASE_URL
        self._access_token = ""
        self._session_key = self._session_iv = None
        self._uid = ""
        self._set_route(await self._request(USER_ROUTE, {
            "userName": encrypt_field(email), "userType": "0", "platformId": "1"}))
        data = await self._request(LOGIN_ID, {"loginAccount": encrypt_field(email), "type": "1"})
        try:
            login_id = data["loginId"]
            if not isinstance(login_id, str) or not login_id:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AccountError() from None
        password_hash = sha256(
            (login_id + sha256(password.encode()).hexdigest() + APP_KEY).encode()).hexdigest()
        result = await self._request(LOGIN, {
            "loginAccount": encrypt_field(email), "password": password_hash, "type": "1"})
        try:
            token = result["mdata"]["accessToken"]
            if not isinstance(token, str) or not token:
                raise ValueError
            if result.get("accessToken") and result.get("randomData"):
                key, iv = _app_material()
                self._session_key = _decrypt(result["accessToken"], key, iv)
                self._session_iv = _decrypt(result["randomData"], key, iv)
            self._access_token = token
            self._uid = str(result.get("uid", ""))
        except (KeyError, TypeError, ValueError):
            raise AccountError() from None

    def encrypt_session_field(self, value):
        if (not self._access_token or self._session_key is None or self._session_iv is None
                or len(self._session_key) not in (16, 24, 32) or len(self._session_iv) != 16):
            raise AccountError()
        return AES.new(self._session_key, AES.MODE_CBC, self._session_iv).encrypt(
            pad(value.encode(), 16)).hex()

    def decrypt_session_field(self, value):
        try:
            return _decrypt(value, self._session_key, self._session_iv).decode()
        except (TypeError, ValueError, UnicodeError):
            raise AccountError() from None

    async def owned_appliance_id(self, serial, *, lan_id=None, native=False):
        """Match a LAN device to the owning account; cloud IDs can differ."""
        data = (await self._request(APPLIANCES, {}, native=True) if native
                else await self._request(APPLIANCES, {}))
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise AccountError()
        matches = set()
        expected_serial = serial.upper() if isinstance(serial, str) else ""
        for item in data["list"]:
            if not isinstance(item, dict):
                continue
            role = item.get("userType", "1")
            owned = str(role) == "1"
            if role is None:
                # Current SmartHome can return a null legacy userType for a
                # newly bound AC. Require both its ownership flag and the
                # exact logged-in owner's UID before accepting that shape.
                owned = (bool(self._uid) and str(item.get("uid", "")) == self._uid
                         and str(item.get("belong", "")) == "1")
            if not owned:
                continue
            item_serial = item.get("sn")
            if (isinstance(item_serial, str) and item_serial.upper() != expected_serial
                    and self._session_key and self._session_iv):
                try:
                    item_serial = self.decrypt_session_field(item_serial)
                except AccountError:
                    continue
            if ((isinstance(item_serial, str) and item_serial and item_serial.upper() == expected_serial)
                    or (not item_serial and lan_id is not None and str(item.get("id")) == str(lan_id))):
                code = str(item.get("id", ""))
                if re.fullmatch(r"[0-9]+", code):
                    matches.add(code)
        if len(matches) > 1:
            raise AccountError(3201)
        return matches.pop() if matches else None

    async def appliance_id(self, device):
        code = await self.owned_appliance_id(device.sn, lan_id=device.id)
        if code is None:
            raise AccountError(3201)
        return code

    async def get_token(self, udpid, appliance_id):
        if (not self._access_token or not re.fullmatch(r"[0-9a-f]{32}", udpid)
                or not re.fullmatch(r"[0-9]+", str(appliance_id))):
            raise AccountError()
        data = await self._request(TOKEN, {"udpid": udpid, "applianceCodes": str(appliance_id)})
        if not isinstance(data, dict) or not isinstance(data.get("tokenlist"), list):
            raise AccountError()
        for item in data["tokenlist"]:
            if isinstance(item, dict) and str(item.get("udpId", "")).lower() == udpid:
                token, key = item.get("token"), item.get("key")
                if (isinstance(token, str) and re.fullmatch(r"[0-9a-fA-F]{128}", token)
                        and isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key)):
                    return token, key
        raise AccountError()

    def close(self):
        self._access_token = self._uid = ""
        self._session_key = self._session_iv = None
