# Midea Connect

[![Code Quality Checks](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/checks.yml/badge.svg)](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/checks.yml)
[![Validate with hassfest](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/hassfest.yml/badge.svg)](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/hassfest.yml)
[![HACS Action](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/hacs.yml/badge.svg)](https://github.com/fiam/home-assistant-midea-connect/actions/workflows/hacs.yml)

Home Assistant custom integration for Midea (and Midea-built) air conditioners with
local LAN control, plus Bluetooth onboarding and cloud-free credential handling.

**Midea Connect is a fork of [mill1000/midea-ac-py](https://github.com/mill1000/midea-ac-py)**
(the `midea_ac` integration) and keeps its entity model and the
[msmart-ng](https://github.com/mill1000/midea-msmart) protocol library. It uses the
integration domain `midea_connect`, so it can be installed alongside the original.
Credit for the device support, entities and protocol work belongs to the upstream
authors; see [LICENSE.md](LICENSE.md).

## What this fork adds

- **Bluetooth onboarding.** Discover nearby ACs through Home Assistant's Bluetooth
  adapters or ESPHome proxies and provision their Wi-Fi from HA, without the vendor app.
- **Explicit cloud accounts for setup only.** Link an AC to a **SmartHome / MSmartHome**
  or **NetHome Plus** account to retrieve its local token and key. Accounts are created
  or logged in from HA; Apple Sign-in is not supported. Startup and normal control never
  contact the cloud.
- **Credential storage and recovery.** Tokens and keys are saved with the entry, can be
  viewed from the device's **Configure** menu, and can be restored from an earlier
  export without cloud access.
- **LAN push.** V3 ACs deliver unsolicited status reports on the existing authenticated
  connection between polls; the configured polling interval remains as a fallback.
- **Optional host Wi-Fi scanning** through a D-Bus service; manual SSID entry always works.

## Installation

### HACS (custom repository)

Add `https://github.com/fiam/home-assistant-midea-connect` as a custom repository of
type *Integration*, install **Midea Connect**, and restart Home Assistant.

### Manual

Copy `custom_components/midea_connect` into your configuration's `custom_components`
directory and restart Home Assistant. To pin a specific commit, download
`https://github.com/fiam/home-assistant-midea-connect/archive/<commit>.tar.gz` and use
the `custom_components/midea_connect` directory inside it.

## Setup

Open **Settings → Devices & services → Add integration → Midea Connect**.

- **Add AC** lists nearby Bluetooth devices, strongest signal first. You can refresh,
  search the LAN, or enter connection details manually. Seeing a device is not proof
  that its controller is in pairing mode. Already networked ACs offer cloud credential
  retrieval or a cloud-free manual/restore path.
- **Setup accounts** manages the two providers separately. Each supports existing
  email/password login and account creation (SmartHome uses an email code, NetHome Plus
  an activation link).
- Network passwords are saved only after successful provisioning, are never prefilled
  in forms, and are included in HA configuration backups.
- Each AC's **Configure** menu offers connection details, advanced settings, and manual
  credential display. Retrying an interrupted setup preserves completed steps.

Migrating from `midea_ac`: back up credentials and note entity IDs before removing old
entries, then set up the accounts and ACs again here. Removing HA entries does not
require factory-resetting ACs or deleting their cloud registrations.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest -q
```

Tests run against `pytest-homeassistant-custom-component`. Contributions follow
[Conventional Commits](https://www.conventionalcommits.org/); see [AGENTS.md](AGENTS.md).

---

The upstream integration documentation follows.

## Upstream documentation

Home Assistant custom integration to control Midea (and associated brands) air conditioners via LAN.

Midea is an OEM for many brands including:
AirCon, Alpine Home Air, Artel, Beko, Canair, Carrier, Century, Comfee, Electrolux, Friedrich, Galactic, Goodman, Hualing, Idea, Inventor, Kaisai, Kenmore, Klimaire, Lennox, LG, Mitsui, Mr. Cool, Neoclima, Olimpia Splendid, Pioneer, Pridiom, QLIMA, Qzen, Rotenso, Royal Clima, Samsung, Senville, Thermocore, Toshiba, Trane and more.

A device is likely supported if it uses one of the following Android apps or it's iOS equivalent.
* Artic King (com.arcticking.ac)
* Midea Air (com.midea.aircondition.obm)
* NetHome Plus (com.midea.aircondition)
* SmartHome/MSmartHome (com.midea.ai.overseas)
* Toshiba AC NA (com.midea.toshiba)
* 美的美居 (com.midea.ai.appliances)

__Note: Only air conditioner devices (type 0xAC and 0xCC) are supported.__

See [Getting Device Info](#getting-device-info) to determine if a device is supported.

## Note On Cloud Usage
This integration works locally. No internet connection is required to control your device.

_However_, for newer "V3" devices, the Midea Cloud is used to acquire a token & key during device discovery. Once configured, the token & key are saved and no further cloud connection is required. Devices are not linked to the built-in accounts. Concerned users may manually configure their devices by acquiring a token & key with their own account credentials via the msmart-ng CLI.

## General Features
* Automatic device discovery and configuration via the GUI.
* Device capability detection. Only supported functions are displayed.
* Minimum and maximum target temperatures provided by the device.

### Device 0xAC Features
* Support for sleep, eco, boost (turbo), and away (freeze protection) presets.
* Switch for device display<sup>1</sup>.
* Device error codes as an attribute.
* Selectable data format and scale for energy and power sensors.<sup>2</sup>
* Advanced indoor/outdoor unit sensors.<sup>2</sup>
* Advanced controls (when supported by the device):
  * Purifier (Ionizer/UV/Sterilizer)
  * Device filter alert
  * Custom fan speeds
  * Out silent mode (PortaSplit)
  * Service to enable the "Follow Me" function<sup>3</sup>
  * Swing angle (fan position)
  * Indoor humidity sensor
  * Target humidity in Dry mode
  * Start and monitor self-cleaning
  * Rate selection (Gear mode)
  * "Breeze" modes (e.g., breeze away, breeze mild, breezeless)
  * iECO (ECOMaster/ECO+)
  * Auxiliary heating mode
  * Flash cool/heat (Jet cool)
  * Cascade
  * Fresh air (ventilation) fan

<small>

1. Device dependent. Some devices only support display control via IR.
2. Sensors must be manually enabled on the device page. A device may not support all sensors.
3. Experimental. "Follow Me" requires the IR remote to transmit temperature data. More info [here](https://github.com/mill1000/midea-msmart/pull/91).
</small>

### Device 0xCC Features
* Support for eco, silent, and sleep presets.
* Advanced controls (when supported by the device):
  * Purifier (Ionizer/UV/Sterilizer)
  * Swing angle (fan position)
  * Indoor humidity sensor
  * Target humidity in Dry mode
  * Auxiliary heating mode

## Translations
Thanks to the community the integration is available in the following languages.
  * български
  * Català
  * Čeština
  * 简体中文
  * 繁體中文
  * Deutsch
  * English
  * Español
  * Euskara
  * Français
  * Hrvatski
  * Italiano
  * 한국어
  * македонски јазик
  * Magyar
  * Nederlands
  * Norsk (bokmål)
  * Polski
  * Português
  * Română
  * Pусский
  * Slovenčina
  * Slovenski
  * Türkçe
  * українська
  * [Help contribute a new language](https://github.com/mill1000/midea-ac-py/issues/54)

## Install Via HACS
Midea Connect is not in the HACS default catalog. Add
`https://github.com/fiam/home-assistant-midea-connect` as a custom repository of type
*Integration*, then install it from HACS.

## Install Manually
1. Locate the `custom_components` directory in your Home Assistant configuration directory. It may need to be created.
2. Copy the `custom_components/midea_connect` directory into the `custom_components` directory.
3. Restart Home Assistant.

## Configuration
Midea Connect is configured via the GUI. See [the HA docs](https://www.home-assistant.io/getting-started/integration/) for more details.

Click the _Add Integration_ button and search for "Midea Connect".

![Add Device](docs/add_device.png)

Devices can be automatically discovered and configured or manually configured.

### Discovery and cloud setup
Choose **Add AC** for nearby Bluetooth ACs, or select **Find ACs on the LAN** for devices already connected to Wi-Fi. LAN discovery itself does not log in to the cloud. After selecting an AC, choose an explicit cloud setup account to retrieve credentials or use existing credentials locally.

### Manual Configuration
For manual configuration, select **Advanced → LAN — enter connection details**.

Enter the device ID, IP, and port. V3 devices require the token and key parameter. This information must be [acquired manually](#getting-device-info).

![Manual Configuration](docs/manual_config.png)

---
Name | Description | Required | Example
:--- | :--- | :--- | :---
**ID** | Device ID | Yes | 123456789012345
**Host** | Device IP address | Yes | 192.168.1.100
**Port** | Device port | Yes | 6444
**Device Type** | Device type | Yes | AC
**Token** | Device token | For V3 devices | ACEDDA53831AE5DC... (128 character hexadecimal string)
**Key** | Device key | For V3 devices | CFFA10FC... (64 character hexadecimal string)

### Backing up and restoring device credentials
The integration saves each device's ID, host, port, token and key in its Home Assistant config entry. A Home Assistant backup that includes the configuration preserves this data. Once valid credentials have been saved, normal setup and control use the local network without signing in again.

No manual backup step is required during setup. To view or copy one AC’s credentials, open that AC’s **Configure → Show device credentials**. This read-only view works even if the AC is offline. Its JSON contains local-control credentials and excludes account passwords and Wi-Fi passwords.

On a fresh Home Assistant installation, choose **Advanced → Restore saved credentials** and paste that JSON. Supply a new host if the IP address has changed. Restore authenticates and checks the AC locally, then saves its credentials in Home Assistant; it never falls back to a cloud login. The AC must be reachable to complete this check. An already-configured AC is left unchanged.

This portable file restores the device connection only; use a Home Assistant backup to preserve entity customizations, integration options and automations too. A factory reset or vendor credential rotation may invalidate old keys; restoring a backup cannot reverse such a device-side change.

## Integration Options
Additional options are available to tweak integration behavior per device. The available options depend on the device type.

---
Name | Default | Device Type | Description
:--- | :--- | :--- | :---
**Update Interval** | 15 | All | Device polling interval in seconds.
**Reverse Horizontal Swing Angle** | False | All | Reverse the order of horizontal swing angles from left-to-right to right-to-left.
**Temperature Step** | 1.0 | All | Step size for temperature set point.
**Maximum Connection Lifetime** | Empty | All | Limit the time (in seconds) a connection to the device will be used before reconnecting. If left blank, the connection will persist indefinitely. If your device disconnects at regular intervals, set this to a value below the interval.
**Device Capability Overrides** | Empty | All | Override or extend the device's reported capabilities. See below for more information.
**Beep** | True | AC | Enable beep on setting changes.
**Fan Speed Step** | 1 | AC | Step size for custom fan speeds.
**Energy Sensor Format > Data Format** | BCD | AC | Select the data format for decoding energy data from the device.
**Energy Sensor Format > Scale** | 1.0 | AC | Select the data scale for reporting energy data from the device.
**Power Sensor Format > Data Format** | BCD | AC | Select the data format for decoding power data from the device.
**Power Sensor Format > Scale** | 1.0 | AC | Select the data scale for reporting power data from the device.
**Workarounds > Use Fan-only Workaround** | False | AC | Enable this option if device updates cause the device to turn on and switch to fan-only.

### Device Capability Overrides
Device capability overrides are an advanced feature that allows you to explicitly define or adjust the capabilities reported by a device. These overrides do **not** enable new functionality. Overrides are intended only to correct or refine incorrectly reported capabilities.

Overrides are specified as YAML key-value pairs. Only the specified keys are affected, any keys not included in the override will retain their existing values. Overrides can either be merged with the detected capabilities or fully replace them.

Refer to the `_SUPPORTED_CAPABILITY_OVERRIDES` property in msmart-ng for supported override keys.

- [AC Device](https://github.com/mill1000/midea-msmart/blob/main/msmart/device/AC/device.py)
- [CC Device](https://github.com/mill1000/midea-msmart/blob/main/msmart/device/CC/device.py)

Example:
```yaml
min_target_temperature: 16
max_target_temperature: 30
supported_modes:
  - AUTO
  - COOL
  - DRY
  - HEAT
supported_capabilities:
  - CUSTOM_FAN_SPEED
  - ECO
  - FREEZE_PROTECTION
  - TURBO
  - IECO
```

### AC Options
![Integration Options](docs/ac_options.png)


### CC Options
![Integration Options](docs/cc_options.png)

## Resolving Connectivity Issues
Some users have reported issue with their devices periodically becoming unavailable, and with logs full of warnings and errors. This is almost always due to the device terminating the existing connection and briefly rejecting new connections.

It can usually be resolved by setting the `Maximum Connection Lifetime` to a value of about 90 seconds.

## Getting Device Info
Use [msmart-ng](https://github.com/mill1000/midea-msmart) to obtain device information.
```shell
pip install msmart-ng
msmart-ng discover
```
Check the output to ensure the type is 0xAC and the `supported` property is True.

## Gratitude
This project is a fork of [mac-zhou/midea-ac-py](https://github.com/mac-zhou/midea-ac-py), and builds upon the work of:
* [NeoAcheron/midea-ac-py](https://github.com/NeoAcheron/midea-ac-py)
* [andersonshatch/midea-ac-py](https://github.com/andersonshatch/midea-ac-py)
* [yitsushi/midea-air-condition](https://github.com/yitsushi/midea-air-condition)

