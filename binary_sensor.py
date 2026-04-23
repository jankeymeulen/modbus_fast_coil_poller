"""Binary sensor platform for Fast Modbus TCP Coil Poller.

Polls Modbus TCP coils at high frequency, tracks previous state,
and updates Home Assistant only when a coil value changes.
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

# ── YAML schema ──────────────────────────────────────────────────────────

COIL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): cv.positive_int,
        vol.Required(CONF_NAME): cv.string,
    }
)

SLAVE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
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


# ── Platform setup ───────────────────────────────────────────────────────


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Modbus coil binary sensors from YAML configuration."""

    pollers: list[ModbusCoilPoller] = []
    all_sensors: list[ModbusCoilBinarySensor] = []

    for slave_cfg in config[CONF_SLAVES]:
        host: str = slave_cfg[CONF_HOST]
        port: int = slave_cfg[CONF_PORT]
        slave_id: int = slave_cfg[CONF_SLAVE_ID]
        interval_s: float = slave_cfg[CONF_SCAN_INTERVAL_MS] / 1000.0
        timeout: float = slave_cfg[CONF_TIMEOUT_S]
        slave_name: str = slave_cfg[CONF_NAME]

        # Build sensor entities for this slave
        sensors: list[ModbusCoilBinarySensor] = []
        for coil_cfg in slave_cfg[CONF_COILS]:
            address: int = coil_cfg[CONF_ADDRESS]
            name: str = coil_cfg[CONF_NAME]
            sensor = ModbusCoilBinarySensor(
                slave_name=slave_name,
                coil_name=name,
                host=host,
                slave_id=slave_id,
                address=address,
            )
            sensors.append(sensor)

        # Create the poller that owns these sensors
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

    # Register all sensor entities at once
    async_add_entities(all_sensors)

    # Start all pollers
    for poller in pollers:
        await poller.async_start()

    # Graceful shutdown: stop pollers and close Modbus connections
    async def _async_shutdown(*_: Any) -> None:
        for poller in pollers:
            await poller.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _async_shutdown)

    _LOGGER.info(
        "modbus_coil_poller: started %d poller(s), %d sensor(s)",
        len(pollers),
        len(all_sensors),
    )


# ── Poller ───────────────────────────────────────────────────────────────


class ModbusCoilPoller:
    """Manages a single Modbus TCP connection and polls coils in a tight loop."""

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
        self._interval = interval
        self._timeout = timeout
        self._sensors = sensors
        self._slave_name = slave_name
        self._client: AsyncModbusTcpClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False

        # Pre-compute the contiguous read range
        addresses = [s.address for s in sensors]
        self._start_address = min(addresses)
        self._coil_count = max(addresses) - self._start_address + 1

        # Map address → sensor for fast lookup
        self._addr_to_sensor: dict[int, ModbusCoilBinarySensor] = {
            s.address: s for s in sensors
        }

    async def async_start(self) -> None:
        """Connect to the Modbus slave and start the polling task."""
        self._client = AsyncModbusTcpClient(
            host=self._host,
            port=self._port,
            timeout=self._timeout,
        )
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
        """High-frequency polling loop."""
        consecutive_errors = 0
        max_backoff = 5.0  # seconds

        while self._running:
            try:
                # Reconnect if needed
                if self._client is None or not self._client.connected:
                    self._client = AsyncModbusTcpClient(
                        host=self._host,
                        port=self._port,
                        timeout=self._timeout,
                    )
                    connected = await self._client.connect()
                    if not connected:
                        raise ConnectionError(
                            f"Cannot connect to {self._host}:{self._port}"
                        )
                    _LOGGER.info("%s: reconnected", self._slave_name)
                    consecutive_errors = 0

                # Batched coil read
                response = await asyncio.wait_for(
                    self._client.read_coils(
                        address=self._start_address,
                        count=self._coil_count,
                        slave=self._slave_id,
                    ),
                    timeout=self._timeout,
                )

                if response.isError():
                    raise RuntimeError(f"Modbus error response: {response}")

                # Distribute results to sensors — only update on change
                for addr, sensor in self._addr_to_sensor.items():
                    idx = addr - self._start_address
                    new_value: bool = response.bits[idx]
                    sensor.update_from_poller(new_value, available=True)

                consecutive_errors = 0

            except asyncio.CancelledError:
                raise  # let the task exit cleanly
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
                # Mark all sensors unavailable on sustained errors
                if consecutive_errors >= 3:
                    for sensor in self._sensors:
                        sensor.update_from_poller(None, available=False)

                await asyncio.sleep(backoff)
                continue

            await asyncio.sleep(self._interval)


# ── Binary sensor entity ─────────────────────────────────────────────────


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
        self._is_on: bool | None = None
        self._available = True

        # Unique & stable identifier
        safe_host = host.replace(".", "_")
        self._attr_unique_id = (
            f"{DOMAIN}_{safe_host}_{slave_id}_{address}"
        )

    # ── Public properties ──

    @property
    def name(self) -> str:
        """Return the display name."""
        return f"{self._slave_name} {self._coil_name}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the coil is energised."""
        return self._is_on

    @property
    def available(self) -> bool:
        """Return True if the Modbus connection is healthy."""
        return self._available

    @property
    def address(self) -> int:
        """Coil address used by the poller for indexing."""
        return self._address

    @property
    def should_poll(self) -> bool:
        """Disable HA's default polling — the poller pushes updates."""
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose diagnostic attributes."""
        return {
            "modbus_host": self._host,
            "modbus_slave_id": self._slave_id,
            "modbus_coil_address": self._address,
        }

    # ── Called by ModbusCoilPoller ──

    @callback
    def update_from_poller(self, value: bool | None, available: bool) -> None:
        """Receive a new coil value from the poller.

        Only triggers a HA state write when the value or availability
        actually changes, keeping the event bus quiet during steady state.
        """
        changed = False

        if available != self._available:
            self._available = available
            changed = True

        if value != self._is_on:
            self._is_on = value
            changed = True

        if changed and self.hass is not None:
            self.async_write_ha_state()
