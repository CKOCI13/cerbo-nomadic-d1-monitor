"""Averaging of voltage, current and power before the Cerbo shows them ([dbus] smoothing).

The converter reports current in whole amps. In a field test (2026-11-13, engine running, D1 boosting
about 2 kW into the 48 V battery) a steady current flipped between 36 and 37 A, so the Overview's
Alternator tile jumped between about 1958 and 2013 W. The GX works out "DC loads" as alternator minus
battery, so that 55 W flicker showed up there as anything from about -58 to +55 W.
"""

import os
import tempfile
import time
import unittest

from support import driver, nc, load_config


def electric(hv_current, hv_voltage=54.3, lv_voltage=13.9, lv_current=0, temperature=40):
    return nc.encode_electric(lv_voltage, lv_current, hv_voltage, hv_current, temperature)


def feed_flicker(t, start, seconds, rate=100):
    """36 / 37 A alternating in a 60/40 mix (a true 36.4 A), 100 frames a second like a real D1."""
    n = int(seconds * rate)
    for i in range(n):
        t.feed(nc.ID_ELECTRIC, electric(37 if i % 5 in (1, 3) else 36), now=start + i / rate)
    return start + (n - 1) / rate


class SmoothingTest(unittest.TestCase):
    def test_shipped_config_and_default(self):
        self.assertEqual(load_config()['dbus'].getfloat('smoothing'), 5.0)
        self.assertEqual(driver.DEFAULTS['dbus']['smoothing'], '5')   # an older config.ini without it

    def test_whole_amp_flicker_is_averaged(self):
        t = driver.Telemetry(60, 5)
        last = feed_flicker(t, 1000.0, 5)
        latest = {'lv_voltage': 13.9, 'lv_current': 0, 'hv_voltage': 54.3, 'hv_current': 36,
                  'temperature': 40}
        a = t.smoothed(latest, now=last)
        self.assertAlmostEqual(a['hv_current'], 36.4, places=2)
        self.assertAlmostEqual(a['hv_power'], 36.4 * 54.3, delta=1)
        self.assertEqual(driver.power(a, 'hv'), round(a['hv_power']))

    def test_old_readings_leave_the_window(self):
        t = driver.Telemetry(60, 5)
        latest = {'lv_voltage': 13.9, 'lv_current': 0, 'hv_voltage': 54.3, 'hv_current': 10,
                  'temperature': 40}
        for i in range(500):   # 5 s at 36 A ...
            t.feed(nc.ID_ELECTRIC, electric(36), now=100 + i / 100)
        for i in range(500):   # ... then 5 s at 10 A: only these are left
            t.feed(nc.ID_ELECTRIC, electric(10), now=105 + i / 100)
        a = t.smoothed(latest, now=110.0)
        self.assertAlmostEqual(a['hv_current'], 10, places=6)

    def test_no_recent_readings_falls_back_to_the_latest(self):
        t = driver.Telemetry(60, 5)
        t.feed(nc.ID_ELECTRIC, electric(36), now=100)
        latest = {'lv_voltage': 13.9, 'lv_current': 0, 'hv_voltage': 54.3, 'hv_current': 36,
                  'temperature': 40}
        a = t.smoothed(latest, now=200)
        self.assertEqual(a['hv_current'], 36)
        self.assertAlmostEqual(a['hv_power'], 36 * 54.3)

    def test_off_shows_the_latest_reading(self):
        t = driver.Telemetry(60, 0)
        t.feed(nc.ID_ELECTRIC, electric(37))
        snap = t.snapshot()
        self.assertEqual(snap['smoothed']['hv_current'], 37)
        self.assertEqual(driver.power(snap['smoothed'], 'hv'), round(37 * 54.3))

    def test_offline_stays_offline(self):
        self.assertIsNone(driver.Telemetry(1, 5).snapshot()['smoothed'])

    def test_temperature_and_history_are_not_averaged(self):
        t = driver.Telemetry(60, 5)
        now = time.monotonic()
        t.feed(nc.ID_ELECTRIC, electric(36, temperature=40), now=now - 1)
        t.feed(nc.ID_ELECTRIC, electric(36, temperature=50), now=now)
        snap = t.snapshot()
        self.assertEqual(snap['smoothed']['temperature'], 50)
        self.assertEqual(snap['electric']['hv_current'], 36)   # 'electric' is still the latest frame
        self.assertEqual(t.take_averages()['temperature'], 45)  # history keeps its own averages


class SmoothedOnTheCerboTest(unittest.TestCase):
    """What the Overview tile (and so DC loads) gets: steady instead of jumping 55 W."""

    def setUp(self):
        self.cfg = load_config()
        tmp = tempfile.mkdtemp()
        self.d = driver.DbusService(self.cfg, driver.UiSettings(os.path.join(tmp, 'ui.json')))

    def powers(self, smoothing):
        t = driver.Telemetry(60, smoothing)
        start = time.monotonic() - 4.98
        seen = []
        # 5 s of 36 / 37 A, refreshing the Cerbo every 0.07 s over the last second
        for i in range(499):
            t.feed(nc.ID_ELECTRIC, electric(37 if i % 5 in (1, 3) else 36), now=start + i / 100)
            if i >= 400 and i % 7 == 0:   # not a multiple of 5, so it catches both values
                self.d.refresh(t.snapshot())
                seen.append(self.d.svc['/Dc/0/Power'])
        return seen

    def test_without_averaging_the_tile_jumps(self):
        seen = self.powers(0)
        self.assertGreaterEqual(max(seen) - min(seen), 50)

    def test_with_averaging_the_tile_is_steady(self):
        seen = self.powers(5)
        self.assertLessEqual(max(seen) - min(seen), 3)
        self.assertAlmostEqual(seen[-1], 36.4 * 54.3, delta=3)
        self.assertAlmostEqual(self.d.svc['/Dc/0/Current'], 36.4, delta=0.05)
        self.assertEqual(self.d.svc['/GenericInput/hv_current/Settings/Decimals'], 1)


if __name__ == '__main__':
    unittest.main()
