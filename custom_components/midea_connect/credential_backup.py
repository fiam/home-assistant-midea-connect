"""Portable per-device credentials; no account data or network operations."""
from __future__ import annotations

import json
import re
from typing import Any

FORMAT = "midea-smart-ac-credentials"
VERSION = 1


def _hex(value: Any, length: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(
        rf"[0-9a-fA-F]{{{length}}}", value
    ):
        raise ValueError("Invalid credential format")
    return value.lower()


def _validate(device: dict[str, Any]) -> dict[str, Any]:
    """Validate and allowlist fields without including input in errors."""
    device_id = device.get("id")
    if isinstance(device_id, bool) or not re.fullmatch(r"[0-9]{1,15}", str(device_id)):
        raise ValueError("Invalid device ID")
    device_id = int(device_id)
    if not 0 < device_id < 2**48:
        raise ValueError("Invalid device ID")
    host = device.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,253}", host):
        raise ValueError("Invalid host")
    port = device.get("port")
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("Invalid port")
    device_type = device.get("device_type")
    if device_type not in ("AC", "CC"):
        raise ValueError("Unsupported device type")
    token = _hex(device.get("token"), 128)
    key = _hex(device.get("key"), 64)
    if bool(token) != bool(key):
        raise ValueError("Token and key must be supplied together")
    return {"id": str(device_id), "host": host, "port": port,
            "device_type": device_type, "token": token, "key": key}


def export_credentials(config: dict[str, Any]) -> str:
    """Export a HA config entry, excluding all account or unrelated fields."""
    try:
        device_type = int(config["device_type"])
        device = _validate({
            "id": config["id"], "host": config["host"], "port": config["port"],
            "device_type": {0xAC: "AC", 0xCC: "CC"}[device_type],
            "token": config.get("token"), "key": config.get("k1"),
        })
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Cannot export incomplete device credentials") from exc
    return json.dumps({"format": FORMAT, "version": VERSION, "device": device}, indent=2)


def import_credentials(value: str) -> dict[str, Any]:
    """Return the existing manual-config schema, using saved LAN keys only."""
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Invalid backup")
    try:
        backup = json.loads(value)
        if (not isinstance(backup, dict) or backup.get("format") != FORMAT
                or type(backup.get("version")) is not int or backup["version"] != VERSION
                or not isinstance(backup.get("device"), dict)):
            raise ValueError("Unsupported backup")
        device = _validate(backup["device"])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError("Invalid credential backup") from exc
    device["k1"] = device.pop("key")
    return device
