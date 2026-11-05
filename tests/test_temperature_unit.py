"""Tests for the web page's temperature unit (follows the GX's own Fahrenheit/Celsius setting)."""

import os
import unittest

from support import driver, load_config, DRIVER_DIR


class TemperatureUnitTest(unittest.TestCase):
    def reader(self, *values):
        calls = []
        values = list(values)

        def read(service, path):
            calls.append((service, path))
            v = values.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        return read, calls

    def test_auto_follows_the_gx_setting(self):
        read, calls = self.reader('celsius', 'fahrenheit')
        u = driver.TemperatureUnit('auto', read)
        self.assertEqual(u.unit, 'F')            # before the first read
        u.poll(now=0)
        self.assertEqual(u.unit, 'C')
        self.assertEqual(calls, [('com.victronenergy.settings', '/Settings/System/Units/Temperature')])
        u.poll(now=5)                            # too soon: not read again
        self.assertEqual(len(calls), 1)
        u.poll(now=10)
        self.assertEqual(u.unit, 'F')

    def test_unreadable_or_odd_values_keep_the_last_unit(self):
        read, _ = self.reader('celsius', RuntimeError('settings restarting'), 'kelvin', '')
        u = driver.TemperatureUnit('auto', read)
        for t in (0, 10, 20, 30):
            u.poll(now=t)
            self.assertEqual(u.unit, 'C')

    def test_without_dbus_auto_shows_fahrenheit(self):
        u = driver.TemperatureUnit('auto', None)
        u.poll(now=0)
        self.assertEqual(u.unit, 'F')

    def test_config_can_force_a_unit(self):
        read, calls = self.reader('fahrenheit')
        for text, unit in (('celsius', 'C'), ('C', 'C'), (' Fahrenheit ', 'F'), ('f', 'F')):
            u = driver.TemperatureUnit(text, read)
            u.poll(now=0)
            self.assertEqual(u.unit, unit)
        self.assertEqual(calls, [])              # forced: the GX setting isn't read

    def test_unknown_config_value_falls_back_to_auto(self):
        read, _ = self.reader('celsius')
        with self.assertLogs('nomadic-dcdc', 'WARNING'):
            u = driver.TemperatureUnit('kelvin', read)
        u.poll(now=0)
        self.assertEqual(u.unit, 'C')

    def test_shipped_config_is_auto(self):
        self.assertEqual(load_config()['web']['temperature_unit'], 'auto')

    def test_web_page_converts_and_labels(self):
        with open(os.path.join(DRIVER_DIR, 'webui.html'), encoding='utf-8') as f:
            page = f.read()
        self.assertIn('c * 9 / 5 + 32', page)
        self.assertIn('s.temperature_unit', page)
        self.assertNotIn("'°C'", page)           # no fixed Celsius label left


if __name__ == '__main__':
    unittest.main()
