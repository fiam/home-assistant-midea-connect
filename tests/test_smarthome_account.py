"""SmartHome's published web protocol, with synthetic cloud responses."""
import json
from hashlib import md5, sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from custom_components.midea_connect import smarthome_account as api
from custom_components.midea_connect.nethome_account import (
    AccountError, RegistrationUncertain)

EMAIL = 'person@example.com'
PASSWORD = 'Example123'
ROUTE = {'masUrl': 'https://mp-eu-prod.appsmb.com/mas/v5/app/proxy?alias='}


def decode_field(value):
    digest = sha256(api.APP_KEY.encode()).hexdigest().encode()
    return unpad(AES.new(digest[:16], AES.MODE_CBC, digest[16:32]).decrypt(bytes.fromhex(value)), 16).decode()


async def test_signup_login_and_owned_token_wire_contract(caplog):
    calls = []
    session_key, session_iv = b'0123456789abcdef', b'fedcba9876543210'
    serial = 'FIXTURE-AC-SERIAL'
    udpid = 'ab' * 16

    def handle(request):
        path = request.url.params['alias']
        calls.append(path)
        body = json.loads(request.content)
        fields = body.get('iotData', body)
        assert fields['appId'] == '1010'
        assert fields['clientType'] == fields['clientSrc'] == 8
        if path == api.REGISTER:
            assert 'sign' not in request.headers and 'random' not in request.headers
        else:
            assert request.headers['sign'] == md5(
                b'meicloud' + request.content + request.headers['random'].encode()).hexdigest()
        if path in (api.LOGIN, api.REGISTER):
            assert body['data']['appKey'] == api.APP_KEY
            assert body['data']['platform'] == 10
        data = None
        code = 0
        if path == api.REGIONS:
            data = {'list': [{'countryCode': 'PT', 'area': 'Portugal'}]}
        elif path == api.USER_ROUTE:
            assert decode_field(fields['userName']) == EMAIL
            if calls.count(path) == 1:
                code = 10004
            else:
                data = ROUTE
        elif path == api.COUNTRY_ROUTE:
            assert fields['countryCode'] == 'PT'
            data = ROUTE
        else:
            assert request.url.host == 'mp-eu-prod.appsmb.com'
            if path == api.SEND_CODE:
                assert fields['type'] == '4'
                assert decode_field(fields['email']) == EMAIL
            elif path == api.VERIFY_CODE:
                assert fields['verifyId'] == '123456'
                assert decode_field(fields['newEmail']) == EMAIL
                data = {'randomCode': 'verification-proof'}
            elif path == api.REGISTER:
                assert decode_field(fields['email']) == EMAIL
                assert decode_field(fields['password']) == sha256(
                    PASSWORD.encode()).hexdigest()
                assert fields['randomCode'] == 'verification-proof'
                assert fields['mobile'] == '' and fields['countryCode'] == 'PT'
            elif path == api.LOGIN_ID:
                assert decode_field(fields['loginAccount']) == EMAIL
                data = {'loginId': 'fixture-login'}
            elif path == api.LOGIN:
                expected = sha256(('fixture-login' + sha256(PASSWORD.encode()
                                                            ).hexdigest() + api.APP_KEY).encode()).hexdigest()
                assert fields['password'] == expected
                data = {'mdata': {'accessToken': 'fixture-session'},
                        'accessToken': api.encrypt_field(session_key.decode()),
                        'randomData': api.encrypt_field(session_iv.decode())}
            elif path == api.APPLIANCES:
                assert request.headers['accessToken'] == 'fixture-session'
                data = {'list': [{'id': '998877', 'userType': 1, 'sn': AES.new(
                    session_key, AES.MODE_CBC, session_iv).encrypt(pad(serial.encode(), 16)).hex()}]}
            elif path == api.TOKEN:
                assert request.headers['accessToken'] == 'fixture-session'
                assert fields['applianceCodes'] == '998877'
                assert fields['udpid'] == udpid
                data = {'tokenlist': [
                    {'udpId': udpid, 'token': '11' * 64, 'key': '22' * 32}]}
            else:
                pytest.fail('Unexpected endpoint')
        return httpx.Response(200, json={'code': code, 'data': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = api.SmartHomeAccountClient(http)
        assert await client.regions() == {'PT': 'Portugal'}
        await client.prepare_registration(EMAIL, 'PT')
        await client.send_code(EMAIL)
        proof = await client.verify_code(EMAIL, '123456')
        await client.register(EMAIL, PASSWORD, 'PT', proof)
        await client.check_login(EMAIL, PASSWORD)
        cloud_id = await client.appliance_id(SimpleNamespace(id=1234, sn=serial))
        assert await client.get_token(udpid, cloud_id) == ('11'*64, '22'*32)
        assert PASSWORD not in repr(vars(client))
    assert calls.count(api.REGISTER) == calls.count(api.SEND_CODE) == 1
    assert EMAIL not in caplog.text and 'fixture-session' not in caplog.text


@pytest.mark.parametrize('endpoint', [api.SEND_CODE, api.REGISTER])
@pytest.mark.parametrize('kind', ['timeout', 'http', 'malformed', 'missing_code', 'list'])
async def test_lost_mutation_response_is_uncertain_without_retry(endpoint, kind, caplog):
    calls = []

    def handle(request):
        calls.append(request)
        if kind == 'timeout':
            raise httpx.ReadTimeout('sensitive response', request=request)
        if kind == 'http':
            return httpx.Response(500, text='sensitive response')
        if kind == 'malformed':
            return httpx.Response(200, text='sensitive response')
        return httpx.Response(200, json=[] if kind == 'list' else {'msg': 'sensitive response'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(RegistrationUncertain) as exc:
            await api.SmartHomeAccountClient(http)._request(endpoint, {})
    assert len(calls) == 1
    assert 'sensitive response' not in str(exc.value) + caplog.text


@pytest.mark.parametrize('url', ['http://mp-prod.appsmb.com/mas/v5/app/proxy',
                                 'https://mp-prod.appsmb.com.evil.example/mas/v5/app/proxy',
                                 'https://user@mp-prod.appsmb.com/mas/v5/app/proxy',
                                 'https://mp-prod.appsmb.com/mas/v5/app/proxy?alias=/other',
                                 'https://mp-prod.appsmb.com/other'])
def test_reject_untrusted_account_route(url):
    with pytest.raises(AccountError):
        api.SmartHomeAccountClient(None)._set_route({'masUrl': url})


@pytest.mark.parametrize('items', [
    [{'id': '1234', 'sn': 'DIFFERENT', 'userType': 1}],
    [{'id': '9988', 'sn': 'MATCH', 'userType': 2}],
    [{'id': '9988', 'sn': 'MATCH'}, {'id': '8877', 'sn': 'MATCH'}],
    [],
])
async def test_require_unambiguous_owned_device(items):
    client = api.SmartHomeAccountClient(None)
    client._request = AsyncMock(return_value={'list': items})
    with pytest.raises(AccountError) as exc:
        await client.appliance_id(SimpleNamespace(id=1234, sn='MATCH'))
    assert exc.value.code == 3201


async def test_existing_account_blocks_registration():
    client = api.SmartHomeAccountClient(None)
    client._request = AsyncMock(return_value=ROUTE)
    with pytest.raises(AccountError) as exc:
        await client.prepare_registration(EMAIL, 'PT')
    assert exc.value.code == 3124
    client._request.assert_awaited_once()


async def test_owned_serial_matches_case_but_never_falls_back_to_another_serial():
    client = api.SmartHomeAccountClient(None)
    client._request = AsyncMock(return_value={'list': [
        {'id': '998877', 'sn': 'fixture-serial', 'userType': 1},
        {'id': '1234', 'sn': 'different-serial', 'userType': 1}]})
    assert await client.appliance_id(SimpleNamespace(sn='FIXTURE-SERIAL', id=1234)) == '998877'


@pytest.mark.parametrize('role,belong,owner,login,expected', [
    (None, 1, 'owner-uid', 'owner-uid', '998877'),
    (None, '1', 'owner-uid', 'owner-uid', '998877'),
    (None, 1, 'other-uid', 'owner-uid', None),
    (None, 0, 'owner-uid', 'owner-uid', None),
    (None, 1, '', '', None),
    (2, 1, 'owner-uid', 'owner-uid', None),
])
async def test_current_account_owner_with_null_legacy_role(role, belong, owner, login, expected):
    client = api.SmartHomeAccountClient(None)
    client._uid = login
    client._request = AsyncMock(return_value={'list': [{
        'id': '998877', 'sn': 'TARGET-SERIAL', 'userType': role,
        'belong': belong, 'uid': owner}]})
    assert await client.owned_appliance_id('TARGET-SERIAL') == expected
    assert await client.owned_appliance_id('DIFFERENT-SERIAL') is None
