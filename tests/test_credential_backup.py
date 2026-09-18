"""Credential backup validation and account-independent round trips."""
import json

import pytest

from custom_components.midea_connect.credential_backup import (
    export_credentials, import_credentials)


def config():
    """Synthetic credentials only."""
    return {"id": 1234, "host": "192.0.2.1", "port": 6444,
            "device_type": 0xAC, "token": "ab" * 64, "k1": "cd" * 32}


def test_round_trip_excludes_account_and_preserves_device_keys():
    original = config() | {"account": "private@example.invalid",
                           "password": "DO-NOT-EXPORT", "wifi_password": "DO-NOT-EXPORT"}
    backup = export_credentials(original)
    assert "private@example.invalid" not in backup
    assert "DO-NOT-EXPORT" not in backup
    assert import_credentials(backup) == {
        "id": "1234", "host": "192.0.2.1", "port": 6444,
        "device_type": "AC", "token": "ab" * 64, "k1": "cd" * 32,
    }


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": -1}, {"id": 2**48}, {"port": 0}, {"port": True},
    {"device_type": "FF"}, {"host": ""}, {"host": "not a host"},
    {"key": "short"}, {"token": "00"}, {"key": None}, {"token": None},
])
def test_invalid_backup_rejected_without_echoing_secrets(change):
    backup = json.loads(export_credentials(config()))
    backup["device"].update(change)
    with pytest.raises(ValueError) as exc:
        import_credentials(json.dumps(backup))
    assert "ab" * 64 not in str(exc.value)
    assert "cd" * 32 not in str(exc.value)


@pytest.mark.parametrize("backup", ["null", "[]", "{}", "invalid", "x" * 8193,
                                    "[" * 2000 + "]" * 2000])
def test_malformed_input(backup):
    with pytest.raises(ValueError):
        import_credentials(backup)


def test_future_backup_version_is_not_silently_accepted():
    backup = json.loads(export_credentials(config()))
    backup["version"] = 2
    with pytest.raises(ValueError):
        import_credentials(json.dumps(backup))


def test_v2_backup_does_not_invent_credentials():
    result = import_credentials(export_credentials(
        config() | {"token": None, "k1": None}))
    assert result["token"] is None
    assert result["k1"] is None
