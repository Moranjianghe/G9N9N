# G9N9N Bluetooth keyboard bridge

Run `./g9n9n-bridge.py` on a Linux system with BlueZ, Python D-Bus/GLib bindings, and python-evdev installed. The user needs access to the system D-Bus and `/dev/uinput`; the remote must already be paired and known to BlueZ. The bridge reconnects and resubscribes when possible.

The default Bluetooth address is `FF:32:3A:75:83:0D`; select another with `--address AA:BB:CC:DD:EE:FF`. By default the bridge looks for a notification characteristic with HID service UUID `00001812-0000-1000-8000-00805f9b34fb` and Report UUID `00002a4d-0000-1000-8000-00805f9b34fb`. Override them with `--service-uuid` and `--characteristic-uuid` if necessary. If more than one characteristic matches, the log lists their paths: select the consumer report with `--characteristic-path /org/bluez/...`. This explicit path can change after device re-pairing or GATT changes; update it when needed.

A held key is released after 10 seconds without another report. Use `--key-timeout SECONDS` to change this, or `--key-timeout 0` to disable the timeout. Some remotes send no further reports during a long press, so increase or disable the timeout if long presses are cut short. Use `--log-level DEBUG`, `INFO`, `WARNING`, or `ERROR` to adjust logging.

Run the simulated behavior tests with `python3 -m unittest discover -s tests -v`. Hardware-specific UUID selection and Bluetooth reconnection should also be verified with the actual remote.
