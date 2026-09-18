"""Device update coordination for Midea Connect."""

import asyncio
import datetime
import logging
from asyncio import Lock
from typing import Generic

from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import (CoordinatorEntity,
                                                      DataUpdateCoordinator)
from msmart.device.AC.command import InvalidResponseException
from msmart.frame import InvalidFrameException

from .const import DOMAIN, UPDATE_INTERVAL, MideaDevice
from .device_proxy import MideaDeviceProxy
from .lan_push import PushAirConditioner

_LOGGER = logging.getLogger(__name__)


class MideaDeviceUpdateCoordinator(DataUpdateCoordinator, Generic[MideaDevice]):
    """Device update coordinator for Midea Connect."""

    def __init__(self, hass: HomeAssistant, device: MideaDevice,
                 update_interval: int = UPDATE_INTERVAL, *, device_name: str | None = None) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=update_interval),
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=1,
                immediate=True,
            )
        )

        self.device_name = device_name
        self.last_local_success = None
        self._lock = Lock()
        self._proxy: MideaDeviceProxy[MideaDevice] = MideaDeviceProxy(device)
        self._energy_sensors = 0
        self._group1_entities = 0
        self._group2_entities = 0
        self._group5_entities = 0
        self._group7_entities = 0
        self._group11_entities = 0
        self._push_device = device if isinstance(
            device, PushAirConditioner) else None
        self._push_task = None
        self._push_stats = {"idle_frames_received": 0, "notification_frames_received": 0,
                            "state_updates": 0, "invalid_frames": 0,
                            "transport_errors": 0, "last_report_at": None}

    def async_start_push(self) -> None:
        """Start after initial authentication/refresh have completed."""
        if self._push_device is not None and self._push_task is None:
            self._push_task = self.hass.async_create_background_task(
                self._async_listen(), f"midea LAN reports {self._push_device.id}")

    @property
    def push_diagnostics(self):
        return {"enabled": self._push_device is not None,
                "listening": self._push_task is not None and not self._push_task.done(),
                **self._push_stats}

    async def _async_listen(self):
        device = self._push_device
        lan = device.push_lan
        while True:
            await lan.received.wait()
            # Commands, refreshes and idle reading share the same lock. A
            # command may have consumed the signal's packet by this point.
            async with self._lock:
                lan.received.clear()
                if lan._protocol is None or not lan._protocol.authenticated:
                    continue
                before = device.to_dict()
                try:
                    async for raw in lan._read_available():
                        try:
                            if not device.apply_lan_report(raw):
                                continue
                        except (InvalidFrameException, InvalidResponseException,
                                ValueError, IndexError, AssertionError, TypeError):
                            self._push_stats["invalid_frames"] += 1
                            continue
                        self.last_local_success = datetime.datetime.now(
                            datetime.timezone.utc).isoformat()
                        self._push_stats["idle_frames_received"] += 1
                        if raw[9] in (4, 5):
                            self._push_stats["notification_frames_received"] += 1
                        self._push_stats["last_report_at"] = datetime.datetime.now(
                            datetime.timezone.utc).isoformat()
                except Exception as exc:
                    # Never log exception text: vendor errors can contain raw
                    # packets. Polling will reconnect using the saved keys.
                    self._push_stats["transport_errors"] += 1
                    _LOGGER.debug("LAN report reader interrupted (%s); polling will reconnect",
                                  type(exc).__name__)
                    lan._disconnect()
                    device._online = False
                if device.to_dict() != before:
                    self._push_stats["state_updates"] += 1
                    # Do not reset the polling timer: partial reports must not
                    # starve full status or optional sensor queries.
                    self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Cancel the idle listener and close its socket on unload/shutdown."""
        await super().async_shutdown()
        if self._push_task is not None:
            self._push_task.cancel()
            await asyncio.gather(self._push_task, return_exceptions=True)
            self._push_task = None
        if self._push_device is not None:
            async with self._lock:
                self._push_device.push_lan._disconnect()

    async def _async_update_data(self) -> None:
        """Update the device data."""
        async with self._lock:
            await self._proxy.refresh()
            if self._proxy.online:
                self.last_local_success = datetime.datetime.now(
                    datetime.timezone.utc).isoformat()

    async def apply(self) -> None:
        """Apply changes to the device and update HA state."""
        self.async_set_updated_data(None)

        async with self._lock:
            await self._proxy.apply()

        await self.async_request_refresh()

    @property
    def device(self) -> MideaDeviceProxy[MideaDevice]:
        """Return the device proxy."""
        return self._proxy

    def register_energy_sensor(self) -> None:
        """Record that an energy sensor is active."""

        if not hasattr(self._proxy, "enable_energy_usage_requests"):
            raise TypeError("Device does not support energy sensors.")

        self._energy_sensors += 1

        # Enable requests
        self._proxy.set_direct("enable_energy_usage_requests", True)

    def unregister_energy_sensor(self) -> None:
        """Record that an energy sensor is inactive."""

        if not hasattr(self._proxy, "enable_energy_usage_requests"):
            raise TypeError("Device does not support energy sensors.")

        self._energy_sensors -= 1

        # Disable requests if last sensor
        self._proxy.set_direct(
            "enable_energy_usage_requests", self._energy_sensors > 0)

    def register_group1_entity(self) -> None:
        """Record that a group1 data entity is active."""
        if not hasattr(self._proxy, "enable_group1_data_requests"):
            raise TypeError("Device does not support group 1 data.")
        self._group1_entities += 1
        self._proxy.set_direct("enable_group1_data_requests", True)

    def unregister_group1_entity(self) -> None:
        """Record that a group1 data entity is inactive."""
        if not hasattr(self._proxy, "enable_group1_data_requests"):
            raise TypeError("Device does not support group 1 data.")
        self._group1_entities -= 1
        self._proxy.set_direct(
            "enable_group1_data_requests", self._group1_entities > 0)

    def register_group2_entity(self) -> None:
        """Record that a group2 data entity is active."""
        if not hasattr(self._proxy, "enable_group2_data_requests"):
            raise TypeError("Device does not support group 2 data.")
        self._group2_entities += 1
        self._proxy.set_direct("enable_group2_data_requests", True)

    def unregister_group2_entity(self) -> None:
        """Record that a group2 data entity is inactive."""
        if not hasattr(self._proxy, "enable_group2_data_requests"):
            raise TypeError("Device does not support group 2 data.")
        self._group2_entities -= 1
        self._proxy.set_direct(
            "enable_group2_data_requests", self._group2_entities > 0)

    def register_group5_entity(self) -> None:
        """Record that a group5 data entity is active."""

        if not hasattr(self._proxy, "enable_group5_data_requests"):
            raise TypeError("Device does not support group 5 data.")

        self._group5_entities += 1

        # Enable requests
        self._proxy.set_direct("enable_group5_data_requests", True)

    def unregister_group5_entity(self) -> None:
        """Record that a group5 data entity is inactive."""

        if not hasattr(self._proxy, "enable_group5_data_requests"):
            raise TypeError("Device does not support group 5 data.")

        self._group5_entities -= 1

        # Disable requests if last entity
        self._proxy.set_direct(
            "enable_group5_data_requests", self._group5_entities > 0)

    def register_group7_entity(self) -> None:
        """Record that a group7 data entity is active."""
        if not hasattr(self._proxy, "enable_group7_data_requests"):
            raise TypeError("Device does not support group 7 data.")
        self._group7_entities += 1
        self._proxy.set_direct("enable_group7_data_requests", True)

    def unregister_group7_entity(self) -> None:
        """Record that a group7 data entity is inactive."""
        if not hasattr(self._proxy, "enable_group7_data_requests"):
            raise TypeError("Device does not support group 7 data.")
        self._group7_entities -= 1
        self._proxy.set_direct(
            "enable_group7_data_requests", self._group7_entities > 0)

    def register_group11_entity(self) -> None:
        """Record that a group11 data entity is active."""
        if not hasattr(self._proxy, "enable_group11_data_requests"):
            raise TypeError("Device does not support group 11 data.")
        self._group11_entities += 1
        self._proxy.set_direct("enable_group11_data_requests", True)

    def unregister_group11_entity(self) -> None:
        """Record that a group11 data entity is inactive."""
        if not hasattr(self._proxy, "enable_group11_data_requests"):
            raise TypeError("Device does not support group 11 data.")
        self._group11_entities -= 1
        self._proxy.set_direct(
            "enable_group11_data_requests", self._group11_entities > 0)


class MideaCoordinatorEntity(CoordinatorEntity[MideaDeviceUpdateCoordinator], Generic[MideaDevice]):
    """Coordinator entity for Midea Connect."""

    def __init__(self, coordinator: MideaDeviceUpdateCoordinator[MideaDevice]) -> None:
        super().__init__(coordinator)

        # Save reference to device
        self._device: MideaDeviceProxy[MideaDevice] = coordinator.device

    @property
    def available(self) -> bool:
        """Check device availability."""
        return self._device.online


class MideaGroup5Entity(MideaCoordinatorEntity):
    """Entity that relies on Group5 data."""

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        # Call super method to ensure lifecycle is properly handled
        await super().async_added_to_hass()

        # Register group 5 sensor with coordinator
        self.coordinator.register_group5_entity()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        # Call super method to ensure lifecycle is properly handled
        await super().async_will_remove_from_hass()

        # Unregister group5 sensor with coordinator
        self.coordinator.unregister_group5_entity()


class MideaGroup1Entity(MideaCoordinatorEntity):
    """Entity that relies on Group 1 data (outdoor unit performance)."""

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.coordinator.register_group1_entity()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        self.coordinator.unregister_group1_entity()


class MideaGroup2Entity(MideaCoordinatorEntity):
    """Entity that relies on Group 2 data (indoor fan data)."""

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.coordinator.register_group2_entity()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        self.coordinator.unregister_group2_entity()


class MideaGroup7Entity(MideaCoordinatorEntity):
    """Entity that relies on Group 7 data (outdoor unit power)."""

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.coordinator.register_group7_entity()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        self.coordinator.unregister_group7_entity()


class MideaGroup11Entity(MideaCoordinatorEntity):
    """Entity that relies on Group 11 data (louver angles)."""

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.coordinator.register_group11_entity()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        self.coordinator.unregister_group11_entity()
