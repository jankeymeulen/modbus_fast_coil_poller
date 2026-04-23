"""Fast Modbus TCP Coil Poller integration for Home Assistant.

This integration polls Modbus TCP slave coils at high frequency (10 Hz+)
and exposes them as binary sensors. It is configured as a platform under
the binary_sensor domain via YAML configuration.
"""
