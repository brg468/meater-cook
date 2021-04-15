"""The Meater Temperature Probe integration."""
from datetime import datetime, timedelta
from enum import Enum
import logging

import async_timeout
from meater import AuthenticationError, TooManyRequestsError

from homeassistant.const import (
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_TIMESTAMP,
    TEMP_CELSIUS,
)
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the entry."""
    # assuming API object stored here by __init__.py
    api = hass.data[DOMAIN][entry.entry_id]

    async def async_update_data():
        """Fetch data from API endpoint."""
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with async_timeout.timeout(10):
                devices = await api.get_all_devices()
        except AuthenticationError as err:
            raise UpdateFailed("The API call wasn't authenticated") from err
        except TooManyRequestsError as err:
            raise UpdateFailed(
                "Too many requests have been made to the API, rate limiting is in place"
            ) from err
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        # Populate the entities
        entities = []

        for dev in devices:
            if dev.id not in hass.data[DOMAIN]["entities"]:
                for name in TemperatureMeasurement:
                    entities.append(MeaterProbeTemperature(coordinator, dev.id, name))

                for name in TimeMeasurement:
                    entities.append(MeaterCookTime(coordinator, dev.id, name))

                for name in CookStates:
                    entities.append(MeaterCookStates(coordinator, dev.id, name))

                device_registry = await dr.async_get_registry(hass)
                device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={(DOMAIN, dev.id)},
                    name=f"Meater Probe {dev.id}",
                    manufacturer="Apption Labs",
                    model="Meater Probe",
                )
                hass.data[DOMAIN]["entities"][dev.id] = None

        async_add_entities(entities)

        return devices

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name="meater_api",
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=timedelta(seconds=30),
    )

    def null_callback():
        return

    # Add a subscriber to the coordinator that doesn't actually do anything, just so that it still updates when all probes are switched off
    coordinator.async_add_listener(null_callback)

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_refresh()


class MeaterSensor(CoordinatorEntity):
    """Base class for Meater Sensors."""

    def __init__(self, device_id, coordinator):
        """Initialize base sensor class."""
        self.device_id = device_id
        super().__init__(coordinator)

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.device_id)
            },
            "name": "Meater Probe",
            "manufacturer": "Apption Labs",
        }


class MeaterProbeTemperature(MeaterSensor):
    """Meater Temperature Sensor Entity."""

    def __init__(self, coordinator, device_id, temperature_reading_type):
        """Initialise the sensor."""
        super().__init__(device_id, coordinator)
        self.temperature_reading_type = temperature_reading_type

    @property
    def name(self):
        """Return the name of the sensor."""
        if (
            self.temperature_reading_type == TemperatureMeasurement.Internal
            or self.temperature_reading_type == TemperatureMeasurement.Ambient
        ):
            return f"Meater Probe {self.temperature_reading_type.name}"
        else:
            return f"Meater Cook {self.temperature_reading_type.name}"

    @property
    def state(self):
        """Return the temperature of the probe."""
        # First find the right probe in the collection
        device = None

        for dev in self.coordinator.data:
            if dev.id == self.device_id:
                device = dev

        if device is None:
            return None

        if TemperatureMeasurement.Internal == self.temperature_reading_type:
            return device.internal_temperature

        elif TemperatureMeasurement.Ambient == self.temperature_reading_type:
            return device.ambient_temperature

        if device.cook is None:
            return None

        if TemperatureMeasurement.Target == self.temperature_reading_type:
            return round(device.cook.target_temperature)

        elif TemperatureMeasurement.Peak == self.temperature_reading_type:
            return device.cook.peak_temperature

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        # Meater API always return temperature in Celsius
        return TEMP_CELSIUS

    @property
    def device_class(self):
        """Return the device class."""
        return DEVICE_CLASS_TEMPERATURE

    @property
    def available(self):
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False

        # See if the device was returned from the API. If not, it's offline
        device = None

        for dev in self.coordinator.data:
            if dev.id == self.device_id:
                device = dev

        if device is None:
            return False

        if (
            self.temperature_reading_type != TemperatureMeasurement.Target
            and self.temperature_reading_type != TemperatureMeasurement.Peak
        ):
            return device is not None
        else:
            return device.cook is not None

    @property
    def unique_id(self):
        """Return the unique ID for the sensor."""
        return f"{self.device_id}-{self.temperature_reading_type}"


class MeaterCookTime(MeaterSensor):
    """Meater Cook Time Entity."""

    def __init__(self, coordinator, device_id, time_measurement):
        """Initialise the sensor."""
        super().__init__(device_id, coordinator)
        self.time_measurement = time_measurement

    @property
    def name(self):
        """Return the name of the sensor."""
        if self.time_measurement.name == "Elapsed":
            return "Meater Cook Began"
        else:
            return "Meater Cook Complete"

    @property
    def state(self):
        """Return the temperature of the probe."""
        # First find the right probe in the collection
        device = None

        now = datetime.now().astimezone()
        for dev in self.coordinator.data:
            if dev.id == self.device_id:
                device = dev

        if device.cook is None:
            return None

        if TimeMeasurement.Elapsed == self.time_measurement:
            # When starting a cook, we occasionally hit this error when time is not defined yet
            try:
                return (
                    now.replace(microsecond=0)
                    - timedelta(seconds=device.cook.time_elapsed)
                ).isoformat()
            except AttributeError:
                return None

        else:
            if device.cook.time_remaining < 0:
                return None
            else:
                return (
                    now.replace(microsecond=0)
                    + timedelta(seconds=device.cook.time_remaining)
                ).isoformat()

    @property
    def unique_id(self):
        """Return the unique ID for the sensor."""
        return f"{self.device_id}-{self.time_measurement}"

    @property
    def device_class(self):
        """Return the device class."""
        return DEVICE_CLASS_TIMESTAMP

    @property
    def available(self):
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False

        # See if the device was returned from the API. If not, it's offline
        return any(
            self.device_id == device.id
            for device in self.coordinator.data
            if device.cook is not None
        )


class MeaterCookStates(MeaterSensor):
    """Meater Cook States."""

    def __init__(self, coordinator, device_id, cook_state):
        super().__init__(device_id, coordinator)
        self.cook_state = cook_state

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"Meater Cook {self.cook_state.name}"

    @property
    def state(self):
        """Return the cook states."""
        # First find the right probe in the collection
        device = None

        for dev in self.coordinator.data:
            if dev.id == self.device_id:
                device = dev

        if device.cook is None:
            return None

        if CookStates.Status == self.cook_state:
            return device.cook.state
        else:
            return device.cook.name

    @property
    def available(self):
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False

        # See if the device was returned from the API. If not, it's offline
        return any(
            self.device_id == device.id
            for device in self.coordinator.data
            if device.cook is not None
        )

    @property
    def unique_id(self):
        """Return the unique ID for the sensor."""
        return f"{self.device_id}-{self.cook_state}"


class TemperatureMeasurement(Enum):
    """Enumeration of possible temperature readings from the probe."""

    Internal = 1
    Ambient = 2
    Target = 3
    Peak = 4


class TimeMeasurement(Enum):
    """Enumeration of possible cook time readings from the probe."""

    Elapsed = 1
    Remaining = 2


class CookStates(Enum):
    """Enumeration of possible cook state readings from the probe."""

    Status = 1
    Name = 2
