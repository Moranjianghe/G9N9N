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
dbus.ByteArray = bytes
mainloop = types.ModuleType('dbus.mainloop')
glib = types.ModuleType('dbus.mainloop.glib')
glib.DBusGMainLoop = lambda **kwargs: None
gi = types.ModuleType('gi')
repository = types.ModuleType('gi.repository')
repository.GLib = types.SimpleNamespace()
evdev = types.ModuleType('evdev')
key_names = ['KEY_ENTER', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT', 'KEY_MUTE', 'KEY_VOLUMEUP', 'KEY_VOLUMEDOWN', 'KEY_PLAYPAUSE', 'KEY_ESC', 'KEY_LEFTMETA', 'KEY_COFFEE', 'KEY_SWITCHVIDEOMODE', 'KEY_PAGEUP', 'KEY_PAGEDOWN']
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
    return {'/dev': {bridge.DEVICE: {'Address': bridge.DEFAULT_ADDRESS, 'Name': 'G9N9N', 'Connected': connected, 'ServicesResolved': resolved}},
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

    def test_voice_close_only_for_valid_button_streams(self):
        sent = []
        self.b.args.cancel_voice = True
        self.b.voice_tx = types.SimpleNamespace(WriteValue=lambda data, options, **kw: sent.append((bytes(data), options)))
        self.b.voice_notification(bytes.fromhex('04 03 02 12'))
        self.b.voice_notification(bytes.fromhex('00 02'))
        self.b.voice_notification(bytes.fromhex('04 03 02'))
        self.b.voice_notification(bytes.fromhex('04 00 02 12'))
        self.assertEqual(sent, [(bytes.fromhex('0d 12'), {'type': 'command'})])

    def test_assistant_does_not_change_volume_keys(self):
        self.b.voice_notification(bytes.fromhex('04 03 02 12'))
        self.value([0xe9, 0]); self.value([0, 0])
        self.value([0xea, 0]); self.value([0, 0])
        self.assertEqual(self.ui.events, [
            (codes.EV_KEY, codes.KEY_VOLUMEUP, 1), (codes.EV_KEY, codes.KEY_VOLUMEUP, 0),
            (codes.EV_KEY, codes.KEY_VOLUMEDOWN, 1), (codes.EV_KEY, codes.KEY_VOLUMEDOWN, 0)])

    def test_voice_discovery_and_disconnect(self):
        self.b.reset('test')
        self.b.args.cancel_voice = True
        self.bus.objects.update({
            '/voice': {bridge.SERVICE: {'Device': '/dev', 'UUID': bridge.ATVV_SERVICE}},
            '/voice/ctl': {bridge.CHAR: {'Service': '/voice', 'UUID': bridge.ATVV_CTL}},
            '/voice/tx': {bridge.CHAR: {'Service': '/voice', 'UUID': bridge.ATVV_TX}},
        })
        self.b.tick()
        self.assertEqual(self.b.voice_ctl_path, '/voice/ctl')
        self.assertIsNotNone(self.b.voice_ctl)
        self.b.properties_changed(bridge.DEVICE, {'Connected': False}, [], path='/dev')
        self.assertIsNone(self.b.voice_ctl)
        self.assertIsNone(self.b.voice_tx)

    def test_stopped_notifications_release_and_resubscribe(self):
        self.value([0x41, 0])
        self.b.properties_changed(bridge.CHAR, {'Notifying': False}, [], path='/char')
        self.assertEqual(self.values(), [1, 0])
        self.assertIsNone(self.b.char)
        self.b.tick()
        self.assertEqual(self.bus.calls.count('notify'), 2)
        self.value([0x41, 0])
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

    def test_known_device_automatically_selects_original_report(self):
        self.b.reset('test')
        self.bus.objects['/dev/service0023/char002a'] = self.bus.objects['/char']
        self.b.tick()
        self.assertEqual(self.b.char_path, '/dev/service0023/char002a')

    def test_name_and_address_selection(self):
        self.b.reset('test')
        self.bus.objects['/dev'][bridge.DEVICE]['Name'] = 'Other'
        self.bus.objects['/dev'][bridge.DEVICE]['Address'] = 'AA:BB:CC:DD:EE:FF'
        self.b.tick()
        self.assertIsNone(self.b.char)
        self.b.args.address = 'AA:BB:CC:DD:EE:FF'
        self.now = self.b.retry_at
        self.b.tick()
        self.assertEqual(self.b.char_path, '/char')

    def test_retry_backoff(self):
        self.b.reset('test'); self.bus.objects = {}
        self.b.tick(); self.assertEqual(self.b.retry_at, 1)
        self.now = 1; self.b.tick(); self.assertEqual(self.b.retry_at, 3)

    def test_extra_buttons(self):
        with patch.object(bridge, 'open_url') as open_url:
            self.value([0x77, 0]); self.value([0x77, 0]); self.value([0, 0])
            self.value([0x78, 0]); self.value([0, 0])
            self.assertEqual([call.args[0] for call in open_url.call_args_list],
                             ['https://www.youtube.com/', 'https://www.netflix.com/'])
        self.value([0x9e, 0x01]); self.value([0, 0])
        self.value([0x89, 0x01]); self.value([0, 0])
        self.assertEqual(self.ui.events, [(codes.EV_KEY, codes.KEY_COFFEE, 1),
                                          (codes.EV_KEY, codes.KEY_COFFEE, 0),
                                          (codes.EV_KEY, codes.KEY_SWITCHVIDEOMODE, 1),
                                          (codes.EV_KEY, codes.KEY_SWITCHVIDEOMODE, 0)])

    def test_removal_and_unmapped(self):
        self.value([0xff, 0xff]); self.assertEqual(self.values(), [])
        self.value([0x41, 0]); self.b.interfaces_removed('/char', [bridge.CHAR])
        self.assertEqual(self.values(), [1, 0])


if __name__ == '__main__': unittest.main()
