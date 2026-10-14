"""Tests for what appears on the Cerbo screen, in VRM and over MQTT (the D-Bus service).

The checks marked "gui-v2" follow the rules in Victron's GUI source code (iochannel.cpp)
that decide whether an item is shown in the switch pane at all.
"""

import os
import tempfile
import unittest

from support import driver, nc, load_config, ELECTRIC_FRAME, PARAMETER_FRAME, VERSION_FRAME


class DbusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = load_config()
        self.ui = driver.UiSettings(os.path.join(self.tmp, 'ui.json'))
        self.d = driver.DbusService(self.cfg, self.ui)
        self.svc = self.d.svc
        self.t = driver.Telemetry(60)
        for can_id, frame in ((nc.ID_ELECTRIC, ELECTRIC_FRAME), (nc.ID_PARAMETER, PARAMETER_FRAME),
                              (nc.ID_VERSION, VERSION_FRAME)):
            self.t.feed(can_id, frame)

    def refresh(self, telemetry=None):
        self.d.refresh((telemetry or self.t).snapshot())

    def channels(self):
        return sorted({p.split('/')[2] for p in self.svc.paths if p.startswith('/GenericInput/')})

    def value(self, path):
        return self.svc.paths[path]['value'] if path in self.svc.paths else None

    def test_service_name_and_registration(self):
        self.assertTrue(self.svc.registered)
        self.assertEqual(self.svc.name, 'com.victronenergy.dcdc.nomadic_can1')
        self.assertIn('monitor only', self.svc['/Mgmt/Connection'])

    def test_nothing_can_be_controlled(self):
        # Only the switch-pane display settings (names, cards, visibility) are writable.
        writable = [p for p, item in self.svc.paths.items() if item['writeable']]
        self.assertTrue(writable)
        for p in writable:
            self.assertTrue(p.startswith('/GenericInput/') and '/Settings/' in p, p)
        self.assertFalse(any(p.startswith('/SwitchableOutput') for p in self.svc.paths))
        self.assertNotIn('/Mode', self.svc.paths)
        self.assertFalse(any('/Control/' in p for p in self.svc.paths))
        self.assertEqual(self.svc.SetValue('/Dc/0/Voltage', 14.0), 1)   # read-only

    def test_every_item_meets_the_gui_v2_rules(self):
        self.refresh(driver.Telemetry(1))   # offline: nothing received yet
        for ch in self.channels():
            base = '/GenericInput/' + ch
            with self.subTest(item=base):
                self.assertTrue(self.svc[base + '/Name'])
                item_type = self.svc[base + '/Settings/Type']
                self.assertTrue(self.svc[base + '/Settings/ValidTypes'] & (1 << item_type))
                labels = self.value(base + '/Settings/Labels')
                if labels is not None:
                    self.assertTrue(labels and all(isinstance(x, str) for x in labels))
                if item_type == driver.INPUT_VALUE:
                    self.assertTrue(self.svc[base + '/Settings/Unit'])

    def test_three_cards(self):
        group = lambda ch: self.svc['/GenericInput/%s/Settings/Group' % ch]
        name = lambda ch: self.svc['/GenericInput/%s/Settings/CustomName' % ch]
        for ch in ('alarm', 'status', 'temperature'):
            self.assertIsNone(group(ch))                 # the device's own card
        for ch in ('lv_voltage', 'lv_current', 'lv_power'):
            self.assertEqual(group(ch), 'DC-DC D1 12 V side')
        for ch in ('hv_voltage', 'hv_current', 'hv_power'):
            self.assertEqual(group(ch), 'DC-DC D1 48 V side')
        # On a named card the GUI would prefix the device name without a custom name
        self.assertEqual((name('lv_voltage'), name('hv_power')), ('Voltage', 'Power'))
        # precharge is hidden by default (the D1 manual doesn't use it)
        self.assertEqual(self.svc['/GenericInput/precharge_status/Settings/ShowUIInput'], 0)
        self.assertEqual(self.svc['/GenericInput/lv_voltage/Settings/ShowUIInput'],
                         driver.SHOW_LOCAL_REMOTE)

    def test_offline_shows_no_communication(self):
        self.refresh(driver.Telemetry(1))
        self.assertEqual(self.svc['/GenericInput/status/Value'], driver.NO_COMMUNICATION)
        self.assertEqual(driver.MODE_LABELS[driver.NO_COMMUNICATION], 'No communication')
        self.assertEqual(self.svc['/GenericInput/status/Status'], 1)
        self.assertIsNone(self.svc['/GenericInput/lv_voltage/Value'])
        self.assertIsNone(self.svc['/GenericInput/lv_power/Value'])
        self.assertIsNone(self.svc['/Dc/0/Voltage'])

    def test_readings_and_alarms(self):
        self.refresh()
        gi = lambda ch, leaf='Value': self.svc['/GenericInput/%s/%s' % (ch, leaf)]
        self.assertEqual((gi('lv_voltage'), gi('hv_current'), gi('temperature')), (13.6, -5, 35))
        self.assertEqual((gi('lv_power'), gi('hv_power')), (272, -256))
        self.assertEqual(driver.MODE_LABELS[gi('status')], 'Buck 48V to 12V')
        self.assertEqual(driver.ALARM_LABELS[gi('alarm')], 'CAN loss')
        self.assertEqual(gi('alarm', 'Status'), 1)
        self.assertEqual(gi('alarm', 'Settings/PrimaryLabel'), '5 active')
        self.assertEqual(self.svc['/Dc/0/Voltage'], 13.6)
        self.assertEqual(self.svc['/Dc/0/Power'], 272)
        self.assertEqual(self.svc['/Dc/In/P'], -256)
        self.assertEqual(self.svc['/FirmwareVersion'], 0x123)
        self.assertEqual(self.svc['/Nomadic/SoftwareVersion'], '1.2.3')
        self.assertEqual(self.svc['/State'], 11)

    def test_renaming_and_hiding_survive_a_restart(self):
        base = '/GenericInput/lv_voltage/Settings/'
        self.assertEqual(self.svc.SetValue(base + 'CustomName', '12V battery'), 0)
        self.assertEqual(self.svc.SetValue(base + 'ShowUIInput', 2), 0)
        self.assertEqual(self.svc.SetValue(base + 'Group', 'x' * 33), 2)   # longer than 32 bytes
        self.assertEqual(self.svc.SetValue(base + 'ShowUIInput', True), 2)
        again = driver.DbusService(self.cfg, driver.UiSettings(os.path.join(self.tmp, 'ui.json')))
        self.assertEqual(again.svc[base + 'CustomName'], '12V battery')
        self.assertEqual(again.svc[base + 'ShowUIInput'], 2)

    def test_switch_pane_can_be_turned_off(self):
        self.cfg['dbus']['switch_pane'] = 'no'
        d = driver.DbusService(self.cfg, self.ui)
        d.refresh(self.t.snapshot())
        self.assertFalse(any(p.startswith('/GenericInput') for p in d.svc.paths))
        self.assertEqual(d.svc['/Dc/0/Voltage'], 13.6)


if __name__ == '__main__':
    unittest.main()
