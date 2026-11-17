"""Idle rule: a parked D1 left in Boost that reports a fixed offset is shown as Idle, 0 A, 0 W.

Field readings (nights of 2026-11-14 and 2026-11-16, engine off, Egis closed): the D1 stayed in
"Boost 12V to 48V" and reported -5/-6 A at 12.6 V and +2 A at 58.0 V for hours. The monitor showed a
steady 116 W on the Overview, DC Loads and History, while the Lithionics battery read 0.0 A / 0.0 W.
"""

import os
import tempfile
import unittest

from support import driver, nc, load_config

PARKED = (driver.MODE_BOOST, 12.6, 6, 58.0, 2)          # as the D1 reports it while parked
DRIVING = (driver.MODE_BOOST, 14.0, 132, 54.2, 30)      # real boost from the alternators


def telemetry(mode, lv_v, lv_i, hv_v, hv_i, idle_current=2.0):
    t = driver.Telemetry(60, idle_current=idle_current)
    t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=mode))
    t.feed(nc.ID_ELECTRIC, nc.encode_electric(lv_v, lv_i, hv_v, hv_i, 28))
    return t


class IdleRuleTest(unittest.TestCase):
    def test_default_is_2_amps(self):
        self.assertEqual(load_config()['dbus'].getfloat('idle_current'), 2.0)

    def test_parked_offset_shows_idle_and_zero_watts(self):
        snap = telemetry(*PARKED).snapshot()
        e, p = snap['electric'], snap['parameter']
        self.assertTrue(e['idle_filtered'])
        self.assertEqual((e['lv_current'], e['hv_current']), (0, 0))
        self.assertEqual((driver.power(e, 'lv'), driver.power(e, 'hv')), (0, 0))
        self.assertEqual((e['lv_voltage'], e['hv_voltage']), (12.6, 58.0))   # voltages untouched
        self.assertEqual(p['operating_mode'], driver.MODE_IDLE)
        self.assertEqual(p['operating_mode_text'], driver.IDLE_TEXT)
        # What the D1 really sent stays available (web page Details)
        self.assertEqual(p['operating_mode_reported'], driver.MODE_BOOST)
        self.assertEqual((e['lv_current_reported'], e['hv_current_reported']), (6, 2))

    def test_real_boost_is_untouched(self):
        snap = telemetry(*DRIVING).snapshot()
        e, p = snap['electric'], snap['parameter']
        self.assertFalse(e['idle_filtered'])
        self.assertEqual((e['lv_current'], e['hv_current']), (-132, 30))
        self.assertEqual(p['operating_mode'], driver.MODE_BOOST)

    def test_3_amps_still_counts(self):
        e = telemetry(driver.MODE_BOOST, 13.8, 14, 54.0, 3).snapshot()['electric']
        self.assertFalse(e['idle_filtered'])
        self.assertEqual(e['hv_current'], 3)

    def test_buck_offset_is_idle_too(self):
        e = telemetry(driver.MODE_BUCK, 12.6, 5, 58.0, 2).snapshot()['electric']
        self.assertTrue(e['idle_filtered'])
        self.assertEqual((e['lv_current'], e['hv_current']), (0, 0))

    def test_zero_turns_it_off(self):
        e = telemetry(*PARKED, idle_current=0).snapshot()['electric']
        self.assertFalse(e['idle_filtered'])
        self.assertEqual((e['lv_current'], e['hv_current']), (-6, 2))

    def test_no_phantom_energy_or_averages(self):
        t = telemetry(*PARKED)
        t.feed(nc.ID_ELECTRIC, nc.encode_electric(12.6, 6, 58.0, 2, 28), now=None)
        self.assertEqual(sum(t.take_energy().values()), 0)
        self.assertEqual(t.take_averages()['hv_current'], 0)

    def test_averaged_readings_are_zero_too(self):
        t = driver.Telemetry(60, smoothing=5, idle_current=2)
        t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=driver.MODE_BOOST))
        for i in range(50):
            t.feed(nc.ID_ELECTRIC, nc.encode_electric(12.6, 6, 58.0, 2, 28), now=100 + i * 0.01)
        a = t.snapshot()['smoothed']
        self.assertEqual((a['lv_power'], a['hv_power']), (0, 0))

    def test_gx_shows_zero_on_the_alternator_tile(self):
        cfg = load_config()
        d = driver.DbusService(cfg, driver.UiSettings(os.path.join(tempfile.mkdtemp(), 'ui.json')))
        d.refresh(telemetry(*PARKED).snapshot())
        self.assertEqual(d.svc['/Dc/0/Power'], 0)
        self.assertEqual(d.svc['/Dc/0/Current'], 0)
        self.assertEqual(d.svc['/Nomadic/OperatingModeText'], driver.IDLE_TEXT)


if __name__ == '__main__':
    unittest.main()
