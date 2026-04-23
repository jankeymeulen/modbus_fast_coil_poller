# Fast Modbus TCP Coil Poller

A custom [Home Assistant](https://www.home-assistant.io/) integration that polls Modbus TCP coils at high frequency (10 Hz and above) and exposes them as binary sensors.

Unlike the built-in Modbus integration, this component is designed for **sub-second polling rates** — ideal for monitoring fast-changing digital I/O on PLCs and industrial controllers where reaction time matters.

---

## Features

- **High-frequency polling** — configurable from 10 ms (100 Hz) to 60 s per slave, default 100 ms (10 Hz)
- **Batched coil reads** — all coils on a slave are read in a single Modbus request for maximum efficiency
- **Change-only state updates** — the HA event bus is only notified when a coil value actually changes, keeping overhead minimal even at high poll rates
- **Multiple slaves** — poll one or more Modbus TCP devices, each with independent settings
- **Auto-reconnect** — exponential backoff on connection errors; sensors go `unavailable` after 3 consecutive failures and recover automatically
- **Graceful shutdown** — connections are properly closed when Home Assistant stops
- **Diagnostic attributes** — each sensor exposes its Modbus host, slave ID, and coil address as state attributes

---

## Installation

1. Copy the `modbus_coil_poller` directory into your Home Assistant `custom_components` folder:

   ```
   <ha-config>/
   └── custom_components/
       └── modbus_coil_poller/
           ├── __init__.py
           ├── binary_sensor.py
           ├── const.py
           └── manifest.json
   ```

2. Restart Home Assistant. The `pymodbus` library (≥ 3.5.0) will be installed automatically.

---

## Configuration

Add the following to your `configuration.yaml`:

```yaml
binary_sensor:
  - platform: modbus_coil_poller
    slaves:
      - name: "PLC 1"
        host: 192.168.1.10
        port: 502                    # optional, default: 502
        slave_id: 1                  # optional, default: 1
        scan_interval_ms: 100        # optional, default: 100 (10 Hz)
        timeout_s: 0.5               # optional, default: 0.5
        coils:
          - address: 0
            name: "Emergency Stop"
          - address: 1
            name: "Door Sensor"
          - address: 5
            name: "Light Relay"

      - name: "PLC 2"
        host: 192.168.1.11
        scan_interval_ms: 50         # 20 Hz
        coils:
          - address: 0
            name: "Pump Running"
          - address: 1
            name: "Valve Open"
```

### Slave configuration

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | ✅ | — | Friendly name prefix for all sensors on this slave (e.g. `"PLC 1"`) |
| `host` | ✅ | — | IP address or hostname of the Modbus TCP device |
| `port` | ❌ | `502` | TCP port |
| `slave_id` | ❌ | `1` | Modbus unit ID |
| `scan_interval_ms` | ❌ | `100` | Poll interval in milliseconds (range: 10–60000) |
| `timeout_s` | ❌ | `0.5` | Read timeout in seconds (range: 0.05–30.0) |
| `coils` | ✅ | — | List of coils to monitor (see below) |

### Coil configuration

| Key | Required | Description |
|---|---|---|
| `address` | ✅ | Modbus coil address (0-based) |
| `name` | ✅ | Sensor name (combined with slave name → `"PLC 1 Emergency Stop"`) |

---

## How it works

### Batched reads

Coils within each slave are **not** read individually. The integration computes the contiguous range covering all configured addresses and issues a single `read_coils(start, count)` request per poll cycle.

For example, if you configure coils at addresses `0`, `1`, and `5`, the integration reads `read_coils(address=0, count=6)` and extracts the three relevant bits. This drastically reduces Modbus traffic compared to individual reads.

### Change detection

Each sensor caches its last known value. After every poll, the new value is compared against the cache. `async_write_ha_state()` is **only** called when the value actually changes. This means even at 100 Hz polling, the HA event bus stays quiet during steady state — events only fire on real transitions.

### Polling architecture

```
┌─────────────────────────────────────────────────────┐
│  Home Assistant event loop                          │
│                                                     │
│   ┌──────────────────┐    ┌──────────────────┐      │
│   │ ModbusCoilPoller │    │ ModbusCoilPoller │      │
│   │   (asyncio.Task) │    │   (asyncio.Task) │      │
│   │                  │    │                  │      │
│   │  ┌────────────┐  │    │  ┌────────────┐  │      │
│   │  │ read_coils │──┼──► │  │ read_coils │──┼──►   │
│   │  └─────┬──────┘  │    │  └─────┬──────┘  │      │
│   │        │ changed? │    │        │ changed? │      │
│   │        ▼         │    │        ▼         │      │
│   │  write_ha_state  │    │  write_ha_state  │      │
│   └──────────────────┘    └──────────────────┘      │
│        ▲                       ▲                    │
│        │ Modbus TCP            │ Modbus TCP         │
└────────┼───────────────────────┼────────────────────┘
         │                       │
    ┌────┴────┐             ┌────┴────┐
    │  PLC 1  │             │  PLC 2  │
    └─────────┘             └─────────┘
```

Each slave gets its own `asyncio.Task` with a dedicated `AsyncModbusTcpClient`. Slaves are polled independently and concurrently — a slow or failing device does not block the others.

### Error handling & reconnection

| Condition | Behaviour |
|---|---|
| Single poll timeout/error | Warning logged, retries on next cycle |
| 3+ consecutive errors | All sensors on that slave marked `unavailable` |
| Connection lost | Auto-reconnect with exponential backoff (capped at 5 s) |
| Connection recovered | Sensors return to `available`, normal polling resumes |
| HA shutdown | All connections gracefully closed |

---

## Entity details

### Naming

Entity names are composed as **`{slave name} {coil name}`**. For example, a slave named `"PLC 1"` with a coil named `"Emergency Stop"` produces a sensor called `PLC 1 Emergency Stop`.

### Unique ID

Each sensor gets a stable unique ID in the format:

```
modbus_coil_poller_{host}_{slave_id}_{address}
```

For example: `modbus_coil_poller_192_168_1_10_1_0`

This allows HA to track the entity across restarts and config changes.

### State attributes

Each binary sensor exposes the following extra attributes:

| Attribute | Example | Description |
|---|---|---|
| `modbus_host` | `192.168.1.10` | IP of the Modbus device |
| `modbus_slave_id` | `1` | Modbus unit ID |
| `modbus_coil_address` | `0` | Coil address |

---

## Performance considerations

- **`scan_interval_ms: 10`** (100 Hz) works well over a local Ethernet connection to a responsive PLC. Adjust upward if you see timeout warnings in the logs.
- **`timeout_s`** should be less than `scan_interval_ms / 1000` to prevent overlapping reads. The default of 0.5 s is appropriate for the default 100 ms interval because the timeout guards against hung connections — normal responses arrive in < 5 ms on LAN.
- Each slave uses one persistent TCP connection. There is **no** connection setup/teardown overhead per poll cycle.
- Large gaps between coil addresses (e.g. coils at address 0 and address 999) still work but will read the entire range. Consider splitting into separate slave entries if the gap is very large and the device doesn't support it efficiently.

---

## Troubleshooting

Enable debug logging for the integration:

```yaml
logger:
  logs:
    custom_components.modbus_coil_poller: debug
```

Common log messages:

| Message | Meaning |
|---|---|
| `started N poller(s), M sensor(s)` | Integration loaded successfully |
| `polling N coils starting at address X every Y ms` | Poller is running |
| `reconnected` | Connection was lost and re-established |
| `poll error (#N), next retry in X s` | Read failed; check network/device |
| `failed to connect to host:port` | Initial connection failed; will retry |

---

## License

This integration is provided as-is for personal use. No warranty expressed or implied.
