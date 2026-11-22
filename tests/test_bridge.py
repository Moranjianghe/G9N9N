import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

# The behavior tests run without Bluetooth, a system bus, or uinput permissions.
dbus = types.ModuleType('dbus')
dbus.DBusException = type('DBusException', (Exception,), {})
dbus.Interface = lambda obj, interface: obj
mainloop = types.ModuleType('dbus.mainloop')
glib = types.ModuleType('dbus.mainloop.glib')
glib.DBusGMainLoop = lambda **kwargs: None
gi = types.ModuleType('gi')
repository = types.ModuleType('gi.repository')
repository.GLib = types.SimpleNamespace()
evdev = types.ModuleType('evdev')
key_names = ['KEY_ENTER', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT', 'KEY_MUTE', 'KEY_VOLUMEUP', 'KEY_VOLUMEDOWN', 'KEY_PLAYPAUSE', 'KEY_ESC', 'KEY_LEFTMETA']
codes = types.SimpleNamespace(EV_KEY=1, KEY={i: name for i, name in enumerate(key_names, 1)}, **{name: i for i, name in enumerate(key_names, 1)})
evdev.ecodes = codes
evdev.UInput = object
modules = {'dbus': dbus, 'dbus.mainloop': mainloop, 'dbus.mainloop.glib': glib, 'gi': gi, 'gi.repository': repository, 'evdev': evdev}
with patch.dict(sys.modules, modules):
    spec = importlib.util.spec_from_file_location('bridge', pathlib.Path(__file__).resolve().parents[1] / 'g9n9n-bridge.py')
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)


class UI:
    def __init__(self): self.events = []
    def write(self, *event): self.events.append(event)
    def syn(self): pass
    def close(self): pass


class Bus:
    def __init__(self): self.calls = []; self.objects = {}; self.handlers = {}
    def get_object(self, name, path): return self
    def GetManagedObjects(self, reply_handler, error_handler): reply_handler(self.objects)
    def Connect(self, reply_handler, error_handler): self.calls.append('connect'); reply_handler()
    def StartNotify(self, reply_handler, error_handler): self.calls.append('notify'); reply_handler()
    def StopNotify(self): self.calls.append('stop')


def objects(connected=True, resolved=True):
    return {'/dev': {bridge.DEVICE: {'Address': bridge.DEFAULT_ADDRESS, 'Connected': connected, 'ServicesResolved': resolved}},
            '/svc': {bridge.SERVICE: {'Device': '/dev', 'UUID': bridge.HID_UUID}},
            '/char': {bridge.CHAR: {'Service': '/svc', 'UUID': bridge.REPORT_UUID, 'Flags': ['notify']}}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.bus, self.ui = Bus(), UI()
        self.bus.objects = objects()
        self.b = bridge.Bridge(self.bus, self.ui, bridge.parse_args([]), lambda: self.now)
        self.b.tick()

    def value(self, data): self.b.properties_changed(bridge.CHAR, {'Value': data}, [], path='/char')
    def values(self): return [x[2] for x in self.ui.events]

    def test_press_switch_release_and_short_report(self):
        self.value([0x41, 0]); self.value([0x41, 0]); self.value([0x42, 0]); self.value([0]); self.value([0, 0])
        self.assertEqual(self.values(), [1, 0, 1, 0])

    def test_disconnect_and_resubscribe(self):
        self.value([0x41, 0])
        self.b.properties_changed(bridge.DEVICE, {'Connected': False}, [], path='/dev')
        self.assertEqual(self.values(), [1, 0])
        self.bus.objects = objects(False)
        self.b.tick()
        self.assertIn('connect', self.bus.calls)
        self.bus.objects = objects()
        self.b.tick()
        self.assertEqual(self.bus.calls.count('notify'), 2)
        self.value([0x41, 0]); self.assertEqual(self.values(), [1, 0, 1])

    def test_timeout_and_repeat_refresh(self):
        self.value([0x41, 0]); self.now = 9; self.value([0x41, 0]); self.now = 10; self.b.tick()
        self.assertEqual(self.values(), [1])
        self.now = 19; self.b.tick(); self.assertEqual(self.values(), [1, 0])
        self.value([0x41, 0]); self.b.args.key_timeout = 0; self.b.deadline = None; self.now = 100; self.b.tick()
        self.assertEqual(self.values(), [1, 0, 1])

    def test_restart_and_stale_callback(self):
        self.value([0x41, 0]); generation = self.b.generation
        self.b.owner_changed(bridge.BLUEZ, ':1', ':2')
        self.b.subscribed(self.bus, generation)
        self.assertIsNone(self.b.char)
        self.assertEqual(self.values(), [1, 0])
        self.b.tick(); self.assertEqual(self.bus.calls.count('notify'), 2)

    def test_ambiguous_characteristics_and_selection(self):
        self.b.reset('test')
        self.bus.objects['/other'] = self.bus.objects['/char']
        self.b.tick(); self.assertIsNone(self.b.char)
        self.b.args.characteristic_path = '/char'; self.now = self.b.retry_at; self.b.tick()
        self.assertEqual(self.b.char_path, '/char')

    def test_retry_backoff(self):
        self.b.reset('test'); self.bus.objects = {}
        self.b.tick(); self.assertEqual(self.b.retry_at, 1)
        self.now = 1; self.b.tick(); self.assertEqual(self.b.retry_at, 3)

    def test_removal_and_unmapped(self):
        self.value([0xff, 0xff]); self.assertEqual(self.values(), [])
        self.value([0x41, 0]); self.b.interfaces_removed('/char', [bridge.CHAR])
        self.assertEqual(self.values(), [1, 0])


if __name__ == '__main__': unittest.main()
