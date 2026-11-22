#!/usr/bin/python3
"""Temporary G9N9N BlueZ GATT -> uinput bridge. Ctrl+C to stop."""
import signal
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from evdev import UInput, ecodes as e

PATH = ("/org/bluez/hci0/dev_FF_32_3A_75_83_0D"
        "/service0023/char002a")
CHAR_IFACE = "org.bluez.GattCharacteristic1"
DEVICE_PATH = PATH.split("/service", 1)[0]
DEVICE_IFACE = "org.bluez.Device1"

# HID Consumer usage -> Linux evdev keycode.
# Unconfirmed physical buttons are intentionally NOT assigned.
KEYS = {
    0x0041: e.KEY_ENTER,
    0x0042: e.KEY_UP,
    0x0043: e.KEY_DOWN,
    0x0044: e.KEY_LEFT,
    0x0045: e.KEY_RIGHT,
    0x00E2: e.KEY_MUTE,
    0x00E9: e.KEY_VOLUMEUP,
    0x00EA: e.KEY_VOLUMEDOWN,
    0x00CD: e.KEY_PLAYPAUSE,
    0x0224: e.KEY_ESC,
    0x0223: e.KEY_LEFTMETA,
}


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    obj = bus.get_object("org.bluez", PATH)
    char = dbus.Interface(obj, CHAR_IFACE)
    ui = UInput({e.EV_KEY: sorted(set(KEYS.values()))}, name="G9N9N Virtual Keyboard")
    loop = GLib.MainLoop()
    pressed_code = 0
    subscribed = False
    matches = []

    def release_current():
        nonlocal pressed_code
        key = KEYS.get(pressed_code)
        if key is not None:
            ui.write(e.EV_KEY, key, 0)
            ui.syn()
        pressed_code = 0

    def changed(interface, props, invalidated):
        nonlocal pressed_code
        if interface != CHAR_IFACE or "Value" not in props:
            return
        raw = bytes(props["Value"])
        if len(raw) < 2:
            print("Ignoring unexpected short notification:", raw.hex(" "), flush=True)
            return
        code = int.from_bytes(raw[:2], "little")
        if code == pressed_code:
            return
        release_current()
        pressed_code = code
        key = KEYS.get(code)
        if key is not None:
            ui.write(e.EV_KEY, key, 1)
            ui.syn()
            print(f"0x{code:04x} -> {e.KEY[key]}", flush=True)
        elif code:
            print(f"Unmapped: 0x{code:04x}", flush=True)

    def device_changed(interface, props, invalidated):
        if interface == DEVICE_IFACE and "Connected" in props and not props["Connected"]:
            release_current()

    def interfaces_removed(path, interfaces):
        if str(path) == DEVICE_PATH and DEVICE_IFACE in interfaces:
            release_current()

    def bluez_owner_changed(name, old_owner, new_owner):
        if name == "org.bluez" and old_owner and not new_owner:
            release_current()

    def stop(*_):
        loop.quit()

    try:
        matches.append(bus.add_signal_receiver(changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged", path=PATH, bus_name="org.bluez"))
        matches.append(bus.add_signal_receiver(device_changed,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged", path=DEVICE_PATH, bus_name="org.bluez"))
        matches.append(bus.add_signal_receiver(interfaces_removed,
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesRemoved", bus_name="org.bluez"))
        matches.append(bus.add_signal_receiver(bluez_owner_changed,
            dbus_interface="org.freedesktop.DBus", signal_name="NameOwnerChanged",
            bus_name="org.freedesktop.DBus", arg0="org.bluez"))
        char.StartNotify()
        subscribed = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        GLib.timeout_add(300, lambda: True)
        print("G9N9N ready. Press remote buttons; Ctrl+C to stop.", flush=True)
        loop.run()
    finally:
        release_current()
        if subscribed:
            try:
                char.StopNotify()
            except dbus.DBusException as exc:
                print("StopNotify:", exc, flush=True)
        for match in matches:
            match.remove()
        ui.close()


if __name__ == "__main__":
    main()
