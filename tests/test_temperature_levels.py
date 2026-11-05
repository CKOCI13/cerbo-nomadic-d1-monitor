"""Tests for the D1's board temperature on every GX screen, and the monitor's warning and alarm.

Limits from the D1 manual (Rev D, section IV.B): full output to 105 degC (221 degF), derating
105-120 degC (221-248 degF), fault above 120 degC (248 degF). The monitor's defaults are a warning
at 100 degC (212 degF) and an alarm at 105 degC (221 degF).
"""

import json
import os
import tempfile
import unittest
import urllib.request

from support import driver, nc, load_config, free_port


def snap(temperature, online=True):
    """A snapshot as Telemetry.snapshot() gives it, with just what the temperature code reads."""
    t = driver.Telemetry(60)
    t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=0x3))
    t.feed(nc.ID_ELECTRIC, nc.encode_electric(13.0, 132, 54.2, 30, temperature))
    s = t.snapshot()
    if not online:
        s.update(online=False, electric=None, parameter=None)
    return s


class TemperatureWatchTest(unittest.TestCase):
    def setUp(self):
        self.w = driver.TemperatureWatch(load_config())

    def levels(self, *temperatures):
        return [self.w.update(snap(c)) for c in temperatures]

    def test_defaults_come_from_the_manual(self):
        self.assertEqual((self.w.warning, self.w.alarm), (100, 105))
        self.assertEqual((driver.D1_DERATE_C, driver.D1_SHUTDOWN_C), (105, 120))
        self.assertEqual(round(driver.c_to_f(100)), 212)
        self.assertEqual(round(driver.c_to_f(105)), 221)

    def test_last_nights_reading_is_fine(self):
        # 180 degF = 82 degC while boosting about 1.7 kW: no warning
        self.assertEqual(self.levels(82), [driver.TEMP_OK])
        self.assertIsNone(self.w.text('F'))

    def test_rising_and_falling_with_hysteresis(self):
        W, A, OK = driver.TEMP_WARNING, driver.TEMP_ALARM, driver.TEMP_OK
        self.assertEqual(self.levels(99, 100, 104, 105, 110), [OK, W, W, A, A])
        # falling: each level holds until 3 degC below its threshold
        self.assertEqual(self.levels(103, 102, 98, 97), [A, W, W, OK])

    def test_offline_clears_it(self):
        self.levels(110)
        self.assertEqual(self.w.update(snap(0, online=False)), driver.TEMP_OK)

    def test_text_in_both_units(self):
        self.levels(101)
        self.assertIn('214 °F', self.w.text('F'))
        self.assertIn('warning level (212 °F)', self.w.text('F'))
        self.assertIn('221 °F', self.w.text('F'))
        self.levels(106)
        self.assertIn('alarm level (105 °C)', self.w.text('C'))
        self.assertIn('stops above 120 °C', self.w.text('C'))

    def test_config_values_and_a_mixed_up_pair(self):
        cfg = load_config()
        cfg['temperature']['warning'] = '95'
        cfg['temperature']['alarm'] = '90'
        w = driver.TemperatureWatch(cfg)
        self.assertEqual((w.warning, w.alarm), (90, 90))


class GxScreensTest(unittest.TestCase):
    def service(self, service_class='alternator', sensor='yes'):
        cfg = load_config()
        cfg['dbus']['service_class'] = service_class
        cfg['temperature']['sensor'] = sensor
        self.ui = driver.UiSettings(os.path.join(tempfile.mkdtemp(), 'ui.json'))
        return driver.DbusService(cfg, self.ui)

    def test_alternator_shows_it_and_raises_the_gx_notification(self):
        d = self.service()
        d.refresh(snap(82))
        self.assertEqual(d.svc['/Dc/0/Temperature'], 82)       # Overview tile + device page
        self.assertEqual(d.svc['/Alarms/HighTemperature'], 0)
        d.refresh(snap(101))
        self.assertEqual(d.svc['/Alarms/HighTemperature'], 1)   # "High Temperature" warning
        d.refresh(snap(106))
        self.assertEqual(d.svc['/Alarms/HighTemperature'], 2)   # alarm

    def test_dcdc_leaves_out_the_battery_temperature_line(self):
        d = self.service('dcdc')
        d.refresh(snap(82))
        self.assertNotIn('/Dc/0/Temperature', d.svc.paths)
        self.assertEqual(d.sensor.svc['/Temperature'], 82)

    def test_temperature_sensor_service(self):
        d = self.service()
        t = d.sensor.svc
        self.assertEqual(t.name, 'com.victronenergy.temperature.nomadic_auto')
        self.assertEqual(t['/DeviceInstance'], 71)
        self.assertEqual(t['/CustomName'], 'DC-DC D1')
        self.assertEqual(t['/TemperatureType'], 5)   # Water heater: 32-212 degF gauge
        self.assertTrue(t.registered)
        d.refresh(snap(82))
        self.assertEqual(t['/Temperature'], 82)
        d.refresh(snap(0, online=False))
        self.assertIsNone(t['/Temperature'])

    def test_sensor_type_can_be_changed_and_is_kept(self):
        d = self.service()
        self.assertEqual(d.sensor.svc.SetValue('/TemperatureType', 2), 0)   # Generic
        self.assertEqual(d.sensor.svc.SetValue('/TemperatureType', 9), 2)   # no such type
        self.assertEqual(d.sensor.svc.SetValue('/TemperatureType', 'hot'), 2)
        again = driver.TemperatureSensor(load_config(), self.ui, type(d.sensor.svc))
        self.assertEqual(again.svc['/TemperatureType'], 2)

    def test_sensor_nothing_else_writable(self):
        d = self.service()
        writable = [p for p, item in d.sensor.svc.paths.items() if item['writeable']]
        self.assertEqual(writable, ['/TemperatureType'])

    def test_sensor_can_be_turned_off(self):
        self.assertIsNone(self.service(sensor='no').sensor)

    def test_switch_pane_alarm_card(self):
        d = self.service()
        labels = d.svc['/GenericInput/alarm/Settings/Labels']
        d.refresh(snap(101))
        self.assertEqual(labels[d.svc['/GenericInput/alarm/Value']], 'Temperature high (warning)')
        self.assertEqual(d.svc['/GenericInput/alarm/Status'], 0)
        d.refresh(snap(106))
        self.assertEqual(labels[d.svc['/GenericInput/alarm/Value']], 'Temperature very high (alarm)')
        self.assertEqual(d.svc['/GenericInput/alarm/Status'], 1)   # red on the card
        d.refresh(snap(82))
        self.assertEqual(d.svc['/GenericInput/alarm/Value'], 0)

    def test_the_d1s_own_alarms_come_first(self):
        d = self.service()
        t = driver.Telemetry(60)
        t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=0x8, fault_status=1))
        t.feed(nc.ID_ELECTRIC, nc.encode_electric(13.0, 50, 54.2, 12, 108))
        d.refresh(t.snapshot())
        labels = d.svc['/GenericInput/alarm/Settings/Labels']
        self.assertEqual(labels[d.svc['/GenericInput/alarm/Value']], 'Power limitation active (derating)')
        self.assertEqual(d.svc['/GenericInput/alarm/Settings/PrimaryLabel'], '2 active')


class WebPageTest(unittest.TestCase):
    def test_status_carries_the_level_and_message(self):
        cfg = load_config()
        t = driver.Telemetry(60)
        t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=0x3))
        t.feed(nc.ID_ELECTRIC, nc.encode_electric(13.0, 132, 54.2, 30, 101))
        app = driver.WebApp(cfg, t, driver.History(t), driver.TemperatureUnit('fahrenheit'))
        s = app.status()
        self.assertEqual(s['temperature_level'], 1)
        self.assertEqual(s['temperature_limits'], {'warning': 100, 'alarm': 105, 'derate': 105, 'shutdown': 120})
        self.assertTrue(any('214 °F' in a for a in s['alarms']))

    def test_page_shows_the_state(self):
        with open(os.path.join(driver.BASE_DIR, 'webui.html')) as f:
            page = f.read()
        self.assertIn('id="temp-state"', page)
        self.assertIn('temperature_limits', page)


if __name__ == '__main__':
    unittest.main()
