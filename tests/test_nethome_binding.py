"""Validate OEM signing/encryption against the recovered app contract."""
import base64
import hmac
import json
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.midea_connect.nethome_account import (APP_KEY,
                                                             AccountError)
from custom_components.midea_connect.nethome_binding import (
    ACCESS_PATH, BIND_PATH, CLIENT_ID, LOOKUP_PATH, NetHomeBindingClient)

SERIAL = "000000P0000000Q1B21A2B3C7E4D0000"


async def test_oem_lookup_and_binding_contract(caplog):
    calls = []
    token = "private-cloud-token"
    account = SimpleNamespace(
        user_id="own-authenticated-id", region_domain="https://mapp-eu.appsmb.com")
    random_code = bytes(range(16))

    def handle(request):
        calls.append(request.url.path)
        assert request.url.host == "eu.dollin.net"
        body = json.loads(request.content)
        sdk = request.url.path == LOOKUP_PATH
        key = sha256(token.encode()).digest()[:16] if sdk else APP_KEY.encode()
        expected = base64.b64encode(hmac.new(
            key, b"POST" + request.url.path.encode() + request.content, sha256).digest()).decode()
        assert request.headers["Signature"] == expected
        if request.url.path == ACCESS_PATH:
            assert body["openUserId"] == account.user_id
            assert request.headers["Authorization"] == ""
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": token}})
        if sdk:
            assert request.headers["Authorization"] == "Bearer " + token
            assert request.headers["SignatureVersion"] == "2.1"
            assert body["randomCode"] == random_code.hex()
            assert body["forceValidRandomCode"] is True
            assert body["clientId"] == CLIENT_ID
            assert isinstance(body["stamp"], str)
            encrypted = bytes.fromhex(body["sn"])
            cipher = Cipher(algorithms.AES(key), modes.CBC(
                encrypted[:16])).decryptor()
            padded = cipher.update(encrypted[16:]) + cipher.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            assert unpadder.update(padded) + \
                unpadder.finalize() == SERIAL.encode()
            return httpx.Response(200, json={"code": 0, "data": {"applianceList": [
                {"applianceCode": "1234", "verificationCode": "private-verification"}]}})
        assert request.url.path == BIND_PATH
        assert request.headers["Authorization"] == token
        assert request.headers["SignatureVersion"] == "2.0"
        assert body["applianceCode"] == "1234"
        assert body["verificationCode"] == "private-verification"
        assert body["oldAppId"] == "1108"
        assert body["applianceType"] == "0xAC"
        return httpx.Response(200, json={"code": 0, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        binding = NetHomeBindingClient(http, account)
        await binding.prepare()
        code, verification = await binding.lookup(SERIAL, random_code)
        await binding.bind(code, verification, "AC 7E4D")
        binding.close()
        assert binding._token is None
    assert calls == [ACCESS_PATH, LOOKUP_PATH, BIND_PATH]
    assert token not in caplog.text and "private-verification" not in caplog.text


async def test_binding_error_is_sanitized_and_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 123, "msg": "private-account-data"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        binding = NetHomeBindingClient(http, SimpleNamespace(
            user_id="own", region_domain="mapp.appsmb.com"))
        binding._token = "private"
        with pytest.raises(AccountError) as exc:
            await binding.bind("1234", "proof", "AC")
    assert len(calls) == 1 and exc.value.code == 123
    assert "private-account-data" not in str(exc.value)


async def test_binding_requires_authenticated_user_identity():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        binding = NetHomeBindingClient(http, SimpleNamespace(
            user_id=None, region_domain="mapp.appsmb.com"))
        with pytest.raises(AccountError):
            await binding.prepare()


async def test_commercial_ac_uses_its_own_prefixed_category():
    def handle(request):
        assert json.loads(request.content)["applianceType"] == "0xCC"
        return httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        binding = NetHomeBindingClient(
            http, SimpleNamespace(region_domain="mapp-eu.appsmb.com"))
        binding._token = "private"
        await binding.bind("1234", "private-proof", "AC", appliance_type="CC")


@pytest.mark.parametrize("data", [None, "", "success", "not-json", {}, True])
async def test_successful_bind_does_not_require_json_data(data):
    """A successful link must advance even when data is an empty/plain string."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 0, "data": data}))) as http:
        binding = NetHomeBindingClient(
            http, SimpleNamespace(region_domain="mapp-eu.appsmb.com"))
        binding._token = "private"
        await binding.bind("1234", "private-proof", "AC")


@pytest.mark.parametrize("result", [{"code": 123, "data": ""}, {"code": 123, "data": None}])
async def test_empty_data_never_hides_server_rejection(result, caplog):
    result["msg"] = "private-account-information"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=result))) as http:
        binding = NetHomeBindingClient(
            http, SimpleNamespace(region_domain="mapp-eu.appsmb.com"))
        binding._token = "private-token"
        with pytest.raises(AccountError) as exc:
            await binding.bind("1234", "private-proof", "AC")
    assert exc.value.code == 123
    assert "code 123" in caplog.text
    assert "private-" not in caplog.text


@pytest.mark.parametrize("path", [ACCESS_PATH, LOOKUP_PATH])
async def test_required_response_data_still_must_be_valid_json(path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 0, "data": ""}))) as http:
        binding = NetHomeBindingClient(
            http, SimpleNamespace(region_domain="mapp-eu.appsmb.com"))
        with pytest.raises(AccountError):
            await binding._request(path, {})
