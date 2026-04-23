"""Constants for the Fast Modbus TCP Coil Poller integration."""

DOMAIN = "modbus_coil_poller"

# Configuration keys
CONF_SLAVES = "slaves"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_SLAVE_ID = "slave_id"
CONF_SCAN_INTERVAL_MS = "scan_interval_ms"
CONF_TIMEOUT_S = "timeout_s"
CONF_COILS = "coils"
CONF_ADDRESS = "address"
CONF_NAME = "name"

# Defaults
DEFAULT_PORT = 502
DEFAULT_SLAVE_ID = 1
DEFAULT_SCAN_INTERVAL_MS = 100  # 10 Hz
DEFAULT_TIMEOUT_S = 0.5
