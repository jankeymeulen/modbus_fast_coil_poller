"""Binary sensor platform for Fast Modbus TCP Coil Poller.

This integration polls Modbus TCP coils (function code 01) at high frequency,
tracks the previous state of each coil, and only pushes updates to Home
Assistant when a value actually changes.  Each coil is exposed as a binary
sensor entity.

Architecture overview:
    - One ModbusCoilPoller per configured Modbus slave.  Each poller owns a
      dedicated asyncio background task that runs a tight read loop.
    - All coils on a slave are read in a single batched read_coils() call
      for efficiency.
    - ModbusCoilBinarySensor entities receive updates from their poller via
      a callback.  They only write state to HA when the value changes,
      keeping the event bus quiet during steady state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.binary_sensor import (
    PLATFORM_SCHEMA,
    BinarySensorEntity,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.framer import FramerType

from .const import (
    CONF_ADDRESS,
    CONF_COILS,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL_MS,
    CONF_SLAVE_ID,
    CONF_SLAVES,
    CONF_TIMEOUT_S,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_MS,
    DEFAULT_SLAVE_ID,
    DEFAULT_TIMEOUT_S,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# YAML configuration schema
#
# Example configuration.yaml entry:
#
#   binary_sensor:
#     - platform: modbus_coil_poller
#       slaves:
#         - name: "PLC 1"
#           host: 192.168.1.10
#           port: 502
#           slave_id: 1
#           scan_interval_ms: 100
#           coils:
#             - address: 0
#               name: "Emergency Stop"
#             - address: 1
#               name: "Door Sensor"
# ─────────────────────────────────────────────────────────────────────────

COIL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): cv.positive_int,  # Modbus coil address (0-based)
        vol.Required(CONF_NAME): cv.string,            # Friendly name for this coil
    }
)

SLAVE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,             # Prefix for all sensor names
        vol.Required(CONF_HOST): cv.string,             # IP or hostname of device
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): cv.positive_int,
        vol.Optional(
            CONF_SCAN_INTERVAL_MS, default=DEFAULT_SCAN_INTERVAL_MS
        ): vol.All(vol.Coerce(int), vol.Range(min=10, max=60000)),
        vol.Optional(CONF_TIMEOUT_S, default=DEFAULT_TIMEOUT_S): vol.All(
            vol.Coerce(float), vol.Range(min=0.05, max=30.0)
        ),
        vol.Required(CONF_COILS): vol.All(cv.ensure_list, [COIL_SCHEMA]),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_SLAVES): vol.All(cv.ensure_list, [SLAVE_SCHEMA]),
    }
)


# ─────────────────────────────────────────────────────────────────────────
# Platform setup — called by Home Assistant when it parses the YAML config
# ─────────────────────────────────────────────────────────────────────────


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Modbus coil binary sensors from YAML configuration."""

    pollers: list[ModbusCoilPoller] = []
    all_sensors: list[ModbusCoilBinarySensor] = []

    # Iterate over each configured Modbus slave
    for slave_cfg in config[CONF_SLAVES]:
        host: str = slave_cfg[CONF_HOST]
        port: int = slave_cfg[CONF_PORT]
        slave_id: int = slave_cfg[CONF_SLAVE_ID]
        interval_s: float = slave_cfg[CONF_SCAN_INTERVAL_MS] / 1000.0
        timeout: float = slave_cfg[CONF_TIMEOUT_S]
        slave_name: str = slave_cfg[CONF_NAME]

        # Create one binary sensor entity per coil
        sensors: list[ModbusCoilBinarySensor] = []
        for coil_cfg in slave_cfg[CONF_COILS]:
            sensor = ModbusCoilBinarySensor(
                slave_name=slave_name,
                coil_name=coil_cfg[CONF_NAME],
                host=host,
                slave_id=slave_id,
                address=coil_cfg[CONF_ADDRESS],
            )
            sensors.append(sensor)

        # Create the poller that will read coils for this slave
        poller = ModbusCoilPoller(
            hass=hass,
            host=host,
            port=port,
            slave_id=slave_id,
            interval=interval_s,
            timeout=timeout,
            sensors=sensors,
            slave_name=slave_name,
        )
        pollers.append(poller)
        all_sensors.extend(sensors)

    # Hand the sensor entities to Home Assistant
    async_add_entities(all_sensors)

    # Start all polling loops
    for poller in pollers:
        await poller.async_start()

    # When HA shuts down, cleanly stop all pollers and close connections
    async def _async_shutdown(*_: Any) -> None:
        for poller in pollers:
            await poller.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _async_shutdown)

    _LOGGER.info(
        "modbus_coil_poller: started %d poller(s), %d sensor(s)",
        len(pollers),
        len(all_sensors),
    )


# ─────────────────────────────────────────────────────────────────────────
# ModbusCoilPoller
#
# Owns a single TCP connection to one Modbus slave and runs a background
# asyncio task that reads all configured coils in one batched request.
# ─────────────────────────────────────────────────────────────────────────


class ModbusCoilPoller:
    """Manages a Modbus TCP connection and polls coils in a tight loop."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        slave_id: int,
        interval: float,
        timeout: float,
        sensors: list[ModbusCoilBinarySensor],
        slave_name: str,
    ) -> None:
        self._hass = hass
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._interval = interval        # seconds between polls
        self._timeout = timeout          # per-request timeout in seconds
        self._sensors = sensors
        self._slave_name = slave_name
        self._client: AsyncModbusTcpClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False

        # Figure out the contiguous address range that covers all configured
        # coils so we can read them in one Modbus request.
        # E.g. coils at [100, 101, 105] → read_coils(address=100, count=6)
        addresses = [s.address for s in sensors]
        self._start_address = min(addresses)
        self._coil_count = max(addresses) - self._start_address + 1

        # Lookup table: coil address → sensor entity, for distributing
        # the response bits to the right sensor after each read
        self._addr_to_sensor: dict[int, ModbusCoilBinarySensor] = {
            s.address: s for s in sensors
        }

    def _create_client(self) -> AsyncModbusTcpClient:
        """Create a new pymodbus async TCP client."""
        return AsyncModbusTcpClient(
            host=self._host,
            port=self._port,
            timeout=self._timeout,
            framer=FramerType.SOCKET,  # standard Modbus TCP framing
        )

    async def async_start(self) -> None:
        """Connect to the Modbus slave and start the polling task."""
        self._client = self._create_client()
        connected = await self._client.connect()
        if not connected:
            _LOGGER.error(
                "%s: failed to connect to %s:%s — will retry in poll loop",
                self._slave_name,
                self._host,
                self._port,
            )

        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())
        _LOGGER.debug(
            "%s: polling %d coils starting at address %d every %.0f ms",
            self._slave_name,
            self._coil_count,
            self._start_address,
            self._interval * 1000,
        )

    async def async_stop(self) -> None:
        """Stop the polling task and close the Modbus connection."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._client is not None:
            self._client.close()
            self._client = None

        _LOGGER.debug("%s: poller stopped", self._slave_name)

    async def _poll_loop(self) -> None:
        """High-frequency polling loop.

        Runs continuously until async_stop() is called.  On connection
        errors it reconnects with exponential backoff.  After 3 consecutive
        failures it marks all sensors as unavailable.
        """
        consecutive_errors = 0
        max_backoff = 5.0  # cap the backoff at 5 seconds

        while self._running:
            try:
                # Reconnect if the connection was lost
                if self._client is None or not self._client.connected:
                    self._client = self._create_client()
                    connected = await self._client.connect()
                    if not connected:
                        raise ConnectionError(
                            f"Cannot connect to {self._host}:{self._port}"
                        )
                    _LOGGER.info("%s: reconnected", self._slave_name)
                    consecutive_errors = 0

                # Read all coils in one batched request (Modbus FC01).
                # "device_id" is pymodbus 3.11+'s name for the unit/slave ID.
                response = await asyncio.wait_for(
                    self._client.read_coils(
                        address=self._start_address,
                        count=self._coil_count,
                        device_id=self._slave_id,
                    ),
                    timeout=self._timeout,
                )

                if response.isError():
                    raise RuntimeError(f"Modbus error response: {response}")

                # Walk through the response bits and push each value to the
                # corresponding sensor.  The sensor itself decides whether
                # the value actually changed and needs a state write.
                for addr, sensor in self._addr_to_sensor.items():
                    bit_index = addr - self._start_address
                    sensor.update_from_poller(
                        value=response.bits[bit_index],
                        available=True,
                    )

                consecutive_errors = 0

            except asyncio.CancelledError:
                # Task was cancelled by async_stop() — exit cleanly
                raise
            except Exception:
                consecutive_errors += 1
                backoff = min(
                    self._interval * (2**consecutive_errors), max_backoff
                )
                _LOGGER.warning(
                    "%s: poll error (#%d), next retry in %.1f s",
                    self._slave_name,
                    consecutive_errors,
                    backoff,
                    exc_info=True,
                )
                # After 3 consecutive failures, mark sensors as unavailable
                # so the HA UI shows them as unhealthy
                if consecutive_errors >= 3:
                    for sensor in self._sensors:
                        sensor.update_from_poller(value=None, available=False)

                await asyncio.sleep(backoff)
                continue  # skip the normal sleep below

            await asyncio.sleep(self._interval)


# ─────────────────────────────────────────────────────────────────────────
# ModbusCoilBinarySensor
#
# A lightweight entity that holds the last-known state of a single coil.
# It does NOT poll on its own (should_poll = False); instead, the
# ModbusCoilPoller pushes new values via update_from_poller().
# ─────────────────────────────────────────────────────────────────────────


class ModbusCoilBinarySensor(BinarySensorEntity):
    """Represents a single Modbus coil as a Home Assistant binary sensor."""

    def __init__(
        self,
        slave_name: str,
        coil_name: str,
        host: str,
        slave_id: int,
        address: int,
    ) -> None:
        self._slave_name = slave_name
        self._coil_name = coil_name
        self._host = host
        self._slave_id = slave_id
        self._address = address

        # Coil value: None means "never read yet" (shows as Unknown in HA)
        self._is_on: bool | None = None
        self._available = True

        # Unique, stable identifier so HA can track this entity across restarts.
        # Dots in the IP are replaced with underscores.
        safe_host = host.replace(".", "_")
        self._attr_unique_id = f"{DOMAIN}_{safe_host}_{slave_id}_{address}"

    @property
    def name(self) -> str:
        """Display name shown in the HA UI, e.g. 'PLC 1 Emergency Stop'."""
        return f"{self._slave_name} {self._coil_name}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the coil is energised (high)."""
        return self._is_on

    @property
    def available(self) -> bool:
        """Return True if the Modbus connection is healthy."""
        return self._available

    @property
    def address(self) -> int:
        """The Modbus coil address — used by the poller for bit indexing."""
        return self._address

    @property
    def should_poll(self) -> bool:
        """Disable HA's built-in polling; the poller pushes updates instead."""
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra attributes shown in the entity detail view."""
        return {
            "modbus_host": self._host,
            "modbus_slave_id": self._slave_id,
            "modbus_coil_address": self._address,
        }

    @callback
    def update_from_poller(self, value: bool | None, available: bool) -> None:
        """Called by the poller whenever it has a new reading for this coil.

        Compares the new value against the cached state.  If nothing changed,
        this is a no-op — no state write, no event on the HA bus.  This keeps
        overhead minimal even at 100 Hz polling.
        """
        changed = False

        if available != self._available:
            self._available = available
            changed = True

        if value != self._is_on:
            self._is_on = value
            changed = True

        # Only write state to HA when something actually changed.
        # async_write_ha_state() triggers the "state_changed" event on the
        # HA event bus, which is what automations and the UI listen to.
        if changed and self.hass is not None:
            self.async_write_ha_state()
