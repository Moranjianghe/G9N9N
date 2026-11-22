#!/usr/bin/python3
"""Bridge a BlueZ GATT consumer report to a virtual keyboard."""
import argparse
import logging
import os
import pwd
import subprocess
import signal
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from evdev import UInput, ecodes as e

BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
SERVICE = "org.bluez.GattService1"
CHAR = "org.bluez.GattCharacteristic1"
DEFAULT_ADDRESS = "FF:32:3A:75:83:0D"
HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"
REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
KEYS = {
    0x0041: e.KEY_ENTER, 0x0042: e.KEY_UP, 0x0043: e.KEY_DOWN,
    0x0044: e.KEY_LEFT, 0x0045: e.KEY_RIGHT, 0x00E2: e.KEY_MUTE,
    0x00E9: e.KEY_VOLUMEUP, 0x00EA: e.KEY_VOLUMEDOWN,
    0x00CD: e.KEY_PLAYPAUSE, 0x0224: e.KEY_ESC, 0x0223: e.KEY_LEFTMETA,
    0x019E: e.KEY_COFFEE, 0x0189: e.KEY_SWITCHVIDEOMODE,
}
URLS = {0x0077: "https://www.youtube.com/", 0x0078: "https://www.netflix.com/"}
LOG = logging.getLogger("g9n9n")


def open_url(url):
    """Open the URL as the invoking desktop user when started with sudo."""
    env = os.environ.copy()
    options = {}
    if os.geteuid() == 0 and env.get("SUDO_UID"):
        user = pwd.getpwuid(int(env["SUDO_UID"]))
        env.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name)
        runtime = f"/run/user/{user.pw_uid}"
        env.setdefault("XDG_RUNTIME_DIR", runtime)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
        options.update(user=user.pw_uid, group=user.pw_gid,
                       extra_groups=os.getgrouplist(user.pw_name, user.pw_gid), cwd=user.pw_dir)
    try:
        subprocess.Popen(["xdg-open", url], env=env, **options)
    except (OSError, ValueError) as exc:
        LOG.error("Cannot open %s: %s", url, exc)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="Bluetooth address (otherwise discover by name)")
    parser.add_argument("--name", default="G9N9N", help="Bluetooth device name (default: G9N9N)")
    parser.add_argument("--service-uuid", default=HID_UUID)
    parser.add_argument("--characteristic-uuid", default=REPORT_UUID)
    parser.add_argument("--characteristic-path", help="Select a specific characteristic when UUIDs are ambiguous")
    parser.add_argument("--key-timeout", type=float, default=10.0,
                        help="Release a held key after this many seconds without a report (0 disables; default: 10)")
    parser.add_argument("--log-level", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], default='INFO')
    args = parser.parse_args(argv)
    if not 0 <= args.key_timeout < float('inf'):
        parser.error('--key-timeout must be a finite non-negative number')
    return args


class Bridge:
    def __init__(self, bus, ui, args, clock=time.monotonic):
        self.bus, self.ui, self.args, self.clock = bus, ui, args, clock
        self.device_path = self.char_path = None
        self.char = None
        self.pressed = 0
        self.deadline = None
        self.pending = False
        self.generation = 0
        self.retry_at = 0
        self.retry_delay = 1
        self.matches = []

    def release(self):
        key = KEYS.get(self.pressed)
        if key is not None:
            self.ui.write(e.EV_KEY, key, 0)
            self.ui.syn()
        self.pressed = 0
        self.deadline = None

    def reset(self, reason):
        LOG.info('%s', reason)
        self.release()
        self.generation += 1
        self.pending = False
        self.device_path = self.char_path = self.char = None
        self.retry_at = self.clock()
        self.retry_delay = 1

    def failed(self, exc, generation):
        if generation != self.generation:
            return
        self.pending = False
        LOG.warning('BlueZ operation failed: %s; retrying in %s seconds', exc, self.retry_delay)
        self.retry_at = self.clock() + self.retry_delay
        self.retry_delay = min(self.retry_delay * 2, 30)

    def tick(self):
        now = self.clock()
        if self.deadline is not None and now >= self.deadline:
            LOG.warning('Key release timed out')
            self.release()
        if not self.char and not self.pending and now >= self.retry_at:
            self.discover()
        return True

    def discover(self):
        generation = self.generation
        self.pending = True
        try:
            manager = dbus.Interface(self.bus.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
            manager.GetManagedObjects(reply_handler=lambda objects: self.discovered(objects, generation),
                                      error_handler=lambda exc: self.failed(exc, generation))
        except dbus.DBusException as exc:
            self.failed(exc, generation)

    def discovered(self, objects, generation):
        if generation != self.generation:
            return
        self.pending = False
        try:
            devices = [(str(p), interfaces[DEVICE]) for p, interfaces in objects.items()
                       if DEVICE in interfaces and (
                           str(interfaces[DEVICE].get('Address', '')).upper() == self.args.address.upper()
                           if self.args.address else
                           self.args.name.casefold() in (str(interfaces[DEVICE].get('Name', '')).casefold(),
                                                        str(interfaces[DEVICE].get('Alias', '')).casefold()))]
            if not devices and not self.args.address and self.args.name == 'G9N9N':
                devices = [(str(p), interfaces[DEVICE]) for p, interfaces in objects.items()
                           if DEVICE in interfaces and str(interfaces[DEVICE].get('Address', '')).upper() == DEFAULT_ADDRESS]
                if devices:
                    LOG.info('Found known remote by address (device name did not match)')
            if len(devices) != 1:
                raise ValueError(f'Expected one device for {self.args.address or self.args.name}, found {len(devices)}; pair it first or use --address')
            device_path, device = devices[0]
            self.device_path = device_path
            if not device.get('Connected', False):
                self.pending = True
                dev = dbus.Interface(self.bus.get_object(BLUEZ, device_path), DEVICE)
                dev.Connect(reply_handler=lambda: self.connected(generation),
                            error_handler=lambda exc: self.failed(exc, generation))
                return
            if not device.get('ServicesResolved', False):
                raise ValueError('Waiting for GATT services to resolve')
            services = {str(p) for p, interfaces in objects.items() if SERVICE in interfaces
                        and str(interfaces[SERVICE].get('Device', '')) == device_path
                        and str(interfaces[SERVICE].get('UUID', '')).lower() == self.args.service_uuid.lower()}
            chars = [str(p) for p, interfaces in objects.items() if CHAR in interfaces
                     and str(interfaces[CHAR].get('Service', '')) in services
                     and str(interfaces[CHAR].get('UUID', '')).lower() == self.args.characteristic_uuid.lower()
                     and ('notify' in interfaces[CHAR].get('Flags', []) or 'indicate' in interfaces[CHAR].get('Flags', []))]
            if self.args.characteristic_path:
                chars = [p for p in chars if p == self.args.characteristic_path]
            elif len(chars) > 1 and str(device.get('Address', '')).upper() == DEFAULT_ADDRESS:
                # This report was used by the original bridge for this specific device.
                known = device_path + '/service0023/char002a'
                if known in chars:
                    chars = [known]
            if len(chars) != 1:
                raise ValueError(f'Expected one matching notification characteristic, found {len(chars)} ({", ".join(chars)}); use --characteristic-path to select one')
            self.char_path = chars[0]
            char = dbus.Interface(self.bus.get_object(BLUEZ, self.char_path), CHAR)
            self.pending = True
            char.StartNotify(reply_handler=lambda: self.subscribed(char, generation),
                             error_handler=lambda exc: self.failed(exc, generation))
        except (dbus.DBusException, ValueError) as exc:
            self.failed(exc, generation)

    def connected(self, generation):
        if generation == self.generation:
            self.pending = False
            self.retry_at = self.clock()
            LOG.info('Connected; waiting for services')

    def subscribed(self, char, generation):
        if generation == self.generation:
            self.pending = False
            self.char = char
            self.retry_delay = 1
            LOG.info('Subscribed to %s', self.char_path)

    def properties_changed(self, interface, props, invalidated, path=None):
        path = str(path)
        if path == self.device_path and interface == DEVICE:
            for name in ('Connected', 'ServicesResolved'):
                if name in props:
                    LOG.debug('Device %s = %s', name, bool(props[name]))
            if ('Connected' in props and not props['Connected']) or ('ServicesResolved' in props and not props['ServicesResolved'] and self.char):
                self.reset('Device disconnected or services lost')
        elif path == self.char_path and interface == CHAR and self.char:
            if 'Notifying' in props:
                LOG.debug('Characteristic Notifying = %s', bool(props['Notifying']))
                if not props['Notifying']:
                    self.reset('Notifications stopped; resubscribing')
                    return
            if 'Value' not in props:
                return
            raw = bytes(props['Value'])
            LOG.debug('Notification from %s: %s', path, raw.hex(' '))
            if len(raw) < 2:
                LOG.warning('Ignoring short notification: %s', raw.hex(' '))
                return
            code = int.from_bytes(raw[:2], 'little')
            if code == self.pressed:
                if code in (*KEYS, *URLS) and self.args.key_timeout:
                    self.deadline = self.clock() + self.args.key_timeout
                return
            self.release()
            self.pressed = code
            key = KEYS.get(code)
            if key is not None:
                self.ui.write(e.EV_KEY, key, 1)
                self.ui.syn()
                if self.args.key_timeout:
                    self.deadline = self.clock() + self.args.key_timeout
                LOG.info('0x%04x -> %s', code, e.KEY[key])
            elif code in URLS:
                if self.args.key_timeout:
                    self.deadline = self.clock() + self.args.key_timeout
                LOG.info('Opening %s', URLS[code])
                open_url(URLS[code])
            elif code:
                LOG.warning('Unmapped: 0x%04x', code)

    def interfaces_removed(self, path, interfaces):
        if (str(path) == self.device_path and DEVICE in interfaces) or (str(path) == self.char_path and CHAR in interfaces):
            self.reset('Device or characteristic removed')

    def owner_changed(self, name, old, new):
        if name == BLUEZ and old and old != new:
            self.reset('BlueZ service restarted or stopped')

    def start(self):
        self.matches.append(self.bus.add_signal_receiver(self.properties_changed, dbus_interface='org.freedesktop.DBus.Properties', signal_name='PropertiesChanged', bus_name=BLUEZ, path_keyword='path'))
        self.matches.append(self.bus.add_signal_receiver(self.interfaces_removed, dbus_interface='org.freedesktop.DBus.ObjectManager', signal_name='InterfacesRemoved', bus_name=BLUEZ))
        self.matches.append(self.bus.add_signal_receiver(self.owner_changed, dbus_interface='org.freedesktop.DBus', signal_name='NameOwnerChanged', bus_name='org.freedesktop.DBus', arg0=BLUEZ))

    def close(self):
        self.release()
        if self.char:
            try:
                self.char.StopNotify()
            except dbus.DBusException as exc:
                LOG.warning('StopNotify: %s', exc)
        for match in self.matches:
            match.remove()
        self.ui.close()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format='%(asctime)s %(levelname)s %(message)s')
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    ui = UInput({e.EV_KEY: sorted(set(KEYS.values()))}, name='G9N9N Virtual Keyboard')
    bridge = Bridge(bus, ui, args)
    loop = GLib.MainLoop()
    try:
        bridge.start()
        signal.signal(signal.SIGINT, lambda *_: loop.quit())
        signal.signal(signal.SIGTERM, lambda *_: loop.quit())
        GLib.timeout_add(250, bridge.tick)
        LOG.info('Waiting for %s', args.address or args.name)
        loop.run()
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
