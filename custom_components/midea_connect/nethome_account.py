"""Setup-only NetHome Plus email registration, based on Android 5.44.0.

This client keeps sessions only in memory and avoids response logging.
The application identifier and signing constant identify the public Android
client; they are not user credentials. Account activation stays enabled.
"""
from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha256
from secrets import token_hex
from urllib.parse import urlsplit

import httpx

APP_ID = "1108"
APP_KEY = "77d8f81ba3d0dc0d2c44081e5ec02975"
BASE_URL = "https://mapp.appsmb.com"
PRIVACY_URL = "https://midea-air-us-east1.oss-us-east-1.aliyuncs.com/GDPR_License/general/english/privacy_notice.html"
TERMS_URL = "https://midea-air-us-east1.oss-us-east-1.aliyuncs.com/GDPR_License/general/english/service_agreement.html"


class AccountError(Exception):
    """A sanitized API failure; response messages may contain account data."""

    def __init__(self, code: int | None = None) -> None:
        super().__init__("NetHome Plus account request failed")
        self.code = code


class RegistrationUncertain(AccountError):
    """The request may have succeeded: do not automatically register again."""


def valid_email(value: str) -> bool:
    """Match the app's email format without changing address case."""
    return re.fullmatch(
        r"(\w[-\w.+]*)@([A-Za-z0-9][-A-Za-z0-9]*\.)+([A-Za-z]{1,14})",
        value, re.ASCII) is not None


def valid_password(value: str) -> bool:
    """Apply the app's 6–20 character, letters-and-digits password rule."""
    return (6 <= len(value) <= 20 and " " not in value
            and re.search(r"[A-Za-z]", value) is not None
            and re.search(r"[0-9]", value) is not None)


class NetHomeAccountClient:
    """A short-lived client used only by the account-creation config flow."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._base_url = BASE_URL
        self._session_id = None
        self.user_id = None

    @property
    def region_domain(self):
        return self._base_url

    async def _request(self, path: str, fields: dict[str, str]):
        if path not in {
            "/v1/region/list", "/v1/user/email/register/new",
            "/v1/user/login/id/get", "/v1/user/login/new",
            "/v1/iot/secure/getToken",
        }:
            raise ValueError("Unsupported account endpoint")
        body = {
            "appId": APP_ID, "src": "17", "format": "2",
            "language": "en_US",
            "stamp": datetime.now().astimezone().strftime("%Y%m%d%H%M%S"),
            **fields,
        }
        if path == "/v1/iot/secure/getToken":
            body.pop("appId")
        material = path + "&".join(
            f"{key}={value}" for key, value in sorted(body.items())) + APP_KEY
        body["sign"] = sha256(material.encode("utf-8")).hexdigest()
        # No retries: registration creates an account and sends activation mail.
        try:
            response = await self._client.post(
                self._base_url + path, data=body, timeout=15, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            code = int(data["errorCode"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            error_type = (RegistrationUncertain if
                          path == "/v1/user/email/register/new" else AccountError)
            raise error_type() from None
        if code:
            raise AccountError(code)
        return data.get("result")

    async def regions(self) -> dict[str, str]:
        """Fetch Midea's region codes, which are not ISO country codes."""
        regions = {}
        for page in range(1, 21):
            data = await self._request("/v1/region/list", {
                "keyword": "", "page": str(page), "pageSize": "100"})
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise AccountError()
            for item in data["list"]:
                if (not isinstance(item, dict)
                    or not isinstance(item.get("regionCode"), str)
                    or not re.fullmatch(r"[0-9]{8}", item["regionCode"])
                    or not isinstance(item.get("name"), str)
                        or not item["name"]):
                    raise AccountError()
                regions[item["regionCode"]] = item["name"]
            # The live API currently returns nextPage=true even on its last
            # page. Honor totalPage as well, rather than looping on that flag.
            last_page = (isinstance(data.get("totalPage"), int)
                         and 0 < data["totalPage"] <= page)
            if data.get("nextPage") is False or last_page:
                if not regions:
                    raise AccountError()
                return regions
        raise AccountError()

    async def register(self, email: str, password: str, region: str) -> None:
        """Request account creation and an activation email exactly once."""
        if not valid_email(email) or not valid_password(password):
            raise ValueError("Invalid account fields")
        if not re.fullmatch(r"[0-9]{8}", region):
            raise ValueError("Invalid region code")
        # g0.i -> d.c.k. With d.f598a enabled, iampwd is omitted.
        await self._request("/v1/user/email/register/new", {
            "email": email, "nickname": email, "regionCode": region,
            "needActive": "true", "password": sha256(password.encode()).hexdigest(),
        })

    async def check_login(self, email: str, password: str) -> None:
        """Verify activation using the current login API and regional routing."""
        self._base_url = BASE_URL
        self._session_id = None
        self.user_id = None
        fields = {"loginAccount": email, "clientType": "1"}
        data = await self._request("/v1/user/login/id/get", fields)
        if not isinstance(data, dict) or not isinstance(data.get("loginId"), str):
            raise AccountError()
        password_hash = sha256(password.encode()).hexdigest()
        login_hash = sha256(
            (data["loginId"] + password_hash + APP_KEY).encode()).hexdigest()
        data = await self._request("/v1/user/login/new", {
            **fields, "password": login_hash, "encryptVersion": "1",
            "pushType": "4", "pushToken": "false", "terminalId": token_hex(8),
        })
        if (not isinstance(data, dict) or not isinstance(data.get("sessionId"), str)
                or not data["sessionId"]):
            raise AccountError()
        # Never follow arbitrary redirect hosts with account/session credentials.
        if domain := data.get("regionDomain"):
            if not isinstance(domain, str):
                raise AccountError()
            url = urlsplit(domain if "://" in domain else "https://" + domain)
            if (url.scheme != "https" or url.username or url.password
                or not re.fullmatch(r"[a-z0-9-]+\.appsmb\.com", url.netloc)
                    or url.path not in ("", "/") or url.query or url.fragment):
                raise AccountError()
            self._base_url = "https://" + url.netloc
        self._session_id = data["sessionId"]
        # Binding always uses the identity returned by this authenticated login.
        if isinstance(data.get("userId"), (str, int)) and not isinstance(data.get("userId"), bool):
            self.user_id = str(data["userId"])

    async def get_token(self, udpid: str) -> tuple[str, str]:
        """Retrieve LAN credentials for one discovered device after login."""
        if not self._session_id or not re.fullmatch(r"[0-9a-f]{32}", udpid):
            raise AccountError()
        data = await self._request("/v1/iot/secure/getToken", {
            "sessionId": self._session_id, "udpid": udpid})
        if not isinstance(data, dict) or not isinstance(data.get("tokenlist"), list):
            raise AccountError()
        for item in data["tokenlist"]:
            if isinstance(item, dict) and item.get("udpId") == udpid:
                token, key = item.get("token"), item.get("key")
                if (isinstance(token, str) and re.fullmatch(r"[0-9a-fA-F]{128}", token)
                        and isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key)):
                    return token, key
        raise AccountError()
