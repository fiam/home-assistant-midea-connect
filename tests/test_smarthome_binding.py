"""Recovered Android binding contract checked with independent HTTP fixtures."""
import hmac
import json
from hashlib import sha256
from unittest.mock import AsyncMock

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from custom_components.midea_connect import smarthome_account as api
from custom_components.midea_connect.nethome_account import AccountError
from custom_components.midea_connect.smarthome_binding import \
    SmartHomeBindingClient

from .test_bluetooth_transport import SERIAL

KEY = b'0123456789abcdef'
IV = b'fedcba9876543210'


def encrypt(value):
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(pad(value.encode(), 16)).hex()


def account(http=None):
    client = api.SmartHomeAccountClient(http)
    client._access_token = 'fixture-session'
    client._uid = 'fixture-uid'
    client._session_key, client._session_iv = KEY, IV
    client._set_route(
        {'masUrl': 'https://mp-eu-prod.appsmb.com/mas/v5/app/proxy'})
    return client


@pytest.mark.parametrize('metadata,model', [('', None), ('000000000000AC002A00', '42')])
async def test_native_lookup_bind_and_confirmation_wire_contract(metadata, model, caplog):
    calls = []
    proof = bytes(range(16))

    def handle(request):
        path = request.url.params['alias']
        calls.append(path)
        body = json.loads(request.content)
        assert request.url.host == 'mp-eu-prod.appsmb.com'
        assert request.headers['accessToken'] == 'fixture-session'
        assert request.headers['secretVersion'] == '1'
        assert request.headers['sign'] == hmac.new(
            b'PROD_VnoClJI9aikS8dyy', b'meicloud' + request.content +
            request.headers['random'].encode(), sha256).hexdigest()
        assert body['appId'] == body['src'] == '1010'
        assert body['clientType'] == 1 and body['format'] == 2
        assert body['uid'] == 'fixture-uid'
        assert len(body['reqId']) == 32
        assert 'clientSrc' not in body
        if path == api.LOOKUP:
            assert unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(
                bytes.fromhex(body['sn'])), 16).decode() == SERIAL
            assert body['forceValidRandomCode'] is True
            assert body['randomCode'] == proof.hex()
            data = {
                'list': [{'sn': encrypt(SERIAL + ('#' + metadata if metadata else ''))}]}
        elif path == api.BIND:
            assert body['referSn'] == encrypt(SERIAL)
            assert body['applianceName'] == 'AC Test'
            assert body['applianceType'] == '0xAC'
            assert body['applianceDes'] == ''
            assert body['timeZoneID'] == 'Europe/Lisbon'
            if model is None:
                assert 'modelNumber' not in body
            else:
                assert body['modelNumber'] == model
            assert not {'randomCode', 'verificationCode',
                        'verificationCodeKey', 'password'} & body.keys()
            data = {'id': '998877', 'modelNumber': model}
        elif path in (api.AUTH_GET, api.AUTH_CONFIRM):
            assert body['applianceCode'] == '998877'
            data = {'status': 3} if path == api.AUTH_GET else None
        elif path == api.APPLIANCES:
            data = {
                'list': [{'id': '998877', 'sn': encrypt(SERIAL), 'userType': 1}]}
        else:
            pytest.fail('Unexpected cloud endpoint')
        return httpx.Response(200, json={'code': 0, 'data': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = account(http)
        binding = SmartHomeBindingClient(client, 'Europe/Lisbon')
        await binding.prepare()
        found = await binding.lookup(SERIAL, proof, 'AC')
        assert found == {'model_number': model}
        assert await binding.bind(SERIAL, 'AC Test', 'AC', **found) == '998877'
        await binding.start_confirmation('998877')
        assert await binding.confirmation_status('998877') == 3
        assert await binding.owned_id(SERIAL) == '998877'
        binding.close()
        assert not client._access_token and client._session_key is None
    assert calls.count(api.BIND) == calls.count(api.AUTH_CONFIRM) == 1
    assert 'fixture-session' not in caplog.text and KEY.decode() not in caplog.text


@pytest.mark.parametrize('value', [
    SERIAL + '#000000000000CC002A00',  # wrong appliance category
    SERIAL + '#000000000000AC',  # truncated model bytes
    SERIAL + '#000000000000AC00ZZZZ',  # invalid metadata
])
async def test_lookup_rejects_bad_matching_metadata(value):
    client = account()
    client._request = AsyncMock(
        return_value={'list': [{'sn': encrypt(value)}]})
    with pytest.raises(AccountError):
        await SmartHomeBindingClient(client, 'UTC').lookup(SERIAL, bytes(16), 'AC')


async def test_lookup_never_selects_a_different_or_ambiguous_device():
    client = account()
    client._request = AsyncMock(
        return_value={'list': [{'sn': encrypt('X'*32)}]})
    binding = SmartHomeBindingClient(client, 'UTC')
    assert await binding.lookup(SERIAL, bytes(16), 'AC') is None
    client._request.return_value = {'list': [{'sn': encrypt(SERIAL)}]*2}
    with pytest.raises(AccountError):
        await binding.lookup(SERIAL, bytes(16), 'AC')


async def test_missing_session_encryption_fails_before_http():
    client = account()
    client._session_key = None
    client._request = AsyncMock()
    with pytest.raises(AccountError):
        await SmartHomeBindingClient(client, 'UTC').prepare()
    client._request.assert_not_called()


@pytest.mark.parametrize('path', [api.BIND, api.AUTH_CONFIRM])
async def test_mutation_timeout_is_sanitized_and_never_retried(path, caplog):
    calls = []

    def handle(request):
        calls.append(request.url.params['alias'])
        raise httpx.ReadTimeout('sensitive fixture body', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AccountError) as exc:
            await account(http)._request(path, {}, native=True)
    assert calls == [path]
    assert exc.value.code is None
    assert 'sensitive fixture body' not in str(exc.value) + caplog.text


@pytest.mark.parametrize('status', [True, '0', 4, None])
async def test_unknown_confirmation_is_not_success(status):
    client = account()
    client._request = AsyncMock(return_value={'status': status})
    with pytest.raises(AccountError):
        await SmartHomeBindingClient(client, 'UTC').confirmation_status('1234')


async def test_confirmation_instructions_are_plain_text():
    client = account()
    client._request = AsyncMock(
        return_value={'confirmDesc': '<p>Press <b>FUNC</b> &amp; confirm.</p>'})
    text = await SmartHomeBindingClient(client, 'UTC').confirmation_instructions(SERIAL, 'AC')
    assert text == 'Press FUNC & confirm.'
