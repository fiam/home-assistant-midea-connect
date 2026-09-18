"""Wire-level account contracts; all HTTP responses are synthetic."""
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest

from custom_components.midea_connect.nethome_account import (
    APP_KEY, AccountError, NetHomeAccountClient, RegistrationUncertain,
    valid_email, valid_password)


def form(request):
    return {k: v[0] for k, v in parse_qs(
        request.content.decode(), keep_blank_values=True).items()}


async def test_register_contract(caplog):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/user/email/register/new"
        data = form(request)
        assert set(data) == {"appId", "src", "format", "language", "stamp", "sign",
                             "email", "nickname", "regionCode", "needActive", "password"}
        assert data["appId"] == "1108"
        assert data["src"] == "17"
        assert data["needActive"] == "true"
        assert data["regionCode"] == "62000000"
        assert data["email"] == data["nickname"] == "person+ha@example.com"
        assert data["password"] == sha256(b"Example123").hexdigest()
        sig = data.pop("sign")
        material = request.url.path + "&".join(
            f"{k}={v}" for k, v in sorted(data.items())) + APP_KEY
        assert sig == sha256(material.encode()).hexdigest()
        return httpx.Response(200, json={"errorCode": "0", "result": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await NetHomeAccountClient(http).register("person+ha@example.com", "Example123", "62000000")
    assert len(calls) == 1
    assert "Example123" not in caplog.text
    assert "person+ha@example.com" not in caplog.text


async def test_regions_pagination():
    pages = []

    def handle(request):
        page = int(form(request)["page"])
        pages.append(page)
        code, name = ("62000000", "Portugal") if page == 1 else (
            "27600000", "Germany")
        return httpx.Response(200, json={"errorCode": 0, "result": {
            "nextPage": True, "totalPage": 2,
            "list": [{"regionCode": code, "name": name}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        assert await NetHomeAccountClient(http).regions() == {
            "62000000": "Portugal", "27600000": "Germany"}
    assert pages == [1, 2]


async def test_login_regional_token_contract(caplog):
    paths = []
    udpid = "ab" * 16

    def handle(request):
        paths.append(request.url.path)
        data = form(request)
        if request.url.path.endswith("id/get"):
            return httpx.Response(200, json={"errorCode": 0, "result": {"loginId": "fixture-id"}})
        if request.url.path.endswith("login/new"):
            assert data["encryptVersion"] == "1"
            assert data["clientType"] == "1"
            expected = sha256(("fixture-id" + sha256(
                b"Example123").hexdigest() + APP_KEY).encode()).hexdigest()
            assert data["password"] == expected
            return httpx.Response(200, json={"errorCode": 0, "result": {
                "sessionId": "fixture-private-session",
                "regionDomain": "https://mapp-eu.appsmb.com"}})
        assert request.url.host == "mapp-eu.appsmb.com"
        assert data["sessionId"] == "fixture-private-session"
        assert data["src"] == "17"
        assert data["udpid"] == udpid
        assert "appId" not in data
        assert "applianceCodes" not in data
        return httpx.Response(200, json={"errorCode": 0, "result": {"tokenlist": [
            {"udpId": "cd" * 16, "token": "33" * 64, "key": "44" * 32},
            {"udpId": udpid, "token": "11" * 64, "key": "22" * 32}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = NetHomeAccountClient(http)
        await client.check_login("person@example.com", "Example123")
        assert await client.get_token(udpid) == ("11" * 64, "22" * 32)
        assert "Example123" not in repr(vars(client))
    assert paths == ["/v1/user/login/id/get",
                     "/v1/user/login/new", "/v1/iot/secure/getToken"]
    assert "fixture-private-session" not in caplog.text


@pytest.mark.parametrize("kind", ["timeout", "http", "malformed", "missing_code"])
async def test_uncertain_registration_never_retries(kind, caplog):
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout("sensitive response text", request=request)
        if kind == "http":
            return httpx.Response(500, text="sensitive response text")
        if kind == "malformed":
            return httpx.Response(200, text="sensitive response text")
        return httpx.Response(200, json={"msg": "sensitive response text"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(RegistrationUncertain) as exc:
            await NetHomeAccountClient(http).register("person@example.com", "Example123", "62000000")
    assert calls == 1
    assert "sensitive response text" not in str(exc.value) + caplog.text


async def test_api_error_sanitized():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                                                               httpx.Response(200, json={"errorCode": 3124,
                                                                                         "msg": "person@example.com exists"}))) as http:
        with pytest.raises(AccountError) as exc:
            await NetHomeAccountClient(http).register("person@example.com", "Example123", "62000000")
        assert exc.value.code == 3124
        assert "person@example.com" not in str(exc.value)


@pytest.mark.parametrize("path", ["/v1/user/delete", "/v1/appliance/bind"])
async def test_endpoint_guard(path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("Unexpected mutation"))) as http:
        with pytest.raises(ValueError):
            await NetHomeAccountClient(http)._request(path, {})


@pytest.mark.parametrize("value,expected", [
    ("abc123", True), ("Example123", True), ("bad", False),
    ("lettersOnly", False), ("123456", False), ("with space1", False),
    ("A1" * 11, False),
])
def test_password_validation(value, expected):
    assert valid_password(value) is expected


@pytest.mark.parametrize("value,expected", [
    ("ha+ac@example.com", True), ("person@example.com", True),
    ("person@", False), ("missing", False), ("has space@example.com", False),
])
def test_email_validation(value, expected):
    assert valid_email(value) is expected
