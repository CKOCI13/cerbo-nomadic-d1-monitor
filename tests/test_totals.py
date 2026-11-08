"""Tests for the running records: the Cerbo's History page and the web page's Records card.

They're kept across restarts, cleared with the Cerbo's "Clear History" button, and never send
anything to the converter.
"""

import json
import os
import tempfile
import unittest

from support import driver, nc, load_config


def electric(lv_v, lv_i, hv_v, hv_i, temp):
    return nc.encode_electric(lv_v, lv_i, hv_v, hv_i, temp)


BOOST = nc.encode_parameter(operating_mode=driver.MODE_BOOST)
BUCK = nc.encode_parameter(operating_mode=driver.MODE_BUCK)
IDLE = nc.encode_parameter(operating_mode=0x2)


class Rig:
    """A Telemetry, Totals and D-Bus service fed with frames at a chosen pace."""

    def __init__(self, path=None):
        self.t = driver.Telemetry(60)
        self.totals = driver.Totals(path)
        self.d = driver.DbusService(load_config(), driver.UiSettings(os.path.join(tempfile.mkdtemp(), 'ui.json')),
                                    totals=self.totals)
        self.now = 1000.0

    def run(self, parameter, electric_frame, seconds, level=0):
        """seconds of the D1 sending this, at 100 readings a second, with a 1-second update."""
        for _ in range(int(seconds)):
            self.t.feed(nc.ID_PARAMETER, parameter, now=self.now)
            for _ in range(100):
                self.now += 0.01
                self.t.feed(nc.ID_ELECTRIC, electric_frame, now=self.now)
            snap = self.t.snapshot()
            self.totals.update(snap, self.t.take_energy(), level, now=self.now)
            self.d.refresh(snap)

    def history(self, name):
        return self.d.svc['/History/' + name]


class TotalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'totals.json')

    def test_boost_energy_and_records(self):
        rig = Rig()
        # Boosting: 13.0 V / 132 A out of the 12 V side, 54.0 V / 30 A into the 48 V battery, for an hour
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 3600)
        v = rig.totals.values
        self.assertAlmostEqual(v['hv_charged'], 54.0 * 30, delta=5)     # Wh
        self.assertAlmostEqual(v['lv_drawn'], 13.0 * 132, delta=5)
        self.assertEqual(v['hv_drawn'], 0)
        self.assertAlmostEqual(v['boost_seconds'], 3600, delta=2)
        self.assertEqual((v['lv_current_peak'], v['hv_current_peak']), (132, 30))
        # Cerbo History page, alternator class: Voltage = 48 V side, Aux voltage = 12 V side
        self.assertAlmostEqual(rig.history('EnergyOut'), 1.62, places=2)   # kWh, "Produced energy"
        self.assertEqual(rig.history('EnergyIn'), 0)
        self.assertEqual(rig.history('MaximumVoltage'), 54.0)
        self.assertEqual(rig.history('MinimumStarterVoltage'), 13.0)
        self.assertEqual(rig.history('MaximumTemperature'), 80)
        self.assertEqual(rig.d.svc['/Settings/HasStarterVoltage'], 1)
        self.assertEqual(rig.d.svc['/Settings/HasTemperature'], 1)

    def test_buck_energy_is_consumed_energy(self):
        rig = Rig()
        rig.run(BUCK, electric(13.4, 60, 52.0, 16, 50), 1800)
        self.assertAlmostEqual(rig.history('EnergyIn'), 52.0 * 16 / 2 / 1000, places=2)
        self.assertEqual(rig.history('EnergyOut'), 0)
        self.assertAlmostEqual(rig.totals.values['buck_seconds'], 1800, delta=2)

    def test_lowest_and_highest(self):
        rig = Rig()
        rig.run(BOOST, electric(12.6, 100, 53.0, 20, 60), 5)
        rig.run(BOOST, electric(14.1, 120, 55.5, 25, 85), 5)
        rig.run(nc.encode_parameter(operating_mode=0x1), electric(0, 0, 0, 0, 20), 5)   # starting up: ignored
        self.assertEqual(rig.history('MinimumStarterVoltage'), 12.6)
        self.assertEqual(rig.history('MaximumStarterVoltage'), 14.1)
        self.assertEqual(rig.history('MinimumVoltage'), 53.0)
        self.assertEqual(rig.history('MaximumVoltage'), 55.5)
        self.assertEqual(rig.history('MinimumTemperature'), 60)
        self.assertEqual(rig.history('MaximumTemperature'), 85)

    def test_gaps_are_not_counted(self):
        rig = Rig()
        rig.run(BOOST, electric(13.0, 100, 54.0, 20, 60), 10)
        before = rig.totals.values['hv_charged']
        rig.now += 30   # the D1 went quiet for half a minute
        rig.run(BOOST, electric(13.0, 100, 54.0, 20, 60), 1)
        self.assertAlmostEqual(rig.totals.values['hv_charged'] - before, 54.0 * 20 / 3600, delta=0.05)

    def test_voltage_warnings_and_faults_count_once_each(self):
        rig = Rig()
        low_lv = nc.encode_parameter(operating_mode=driver.MODE_BOOST, voltage_status=0b10)
        fault = nc.encode_parameter(operating_mode=0x2, faults=1 << 7)   # product overheating
        frame = electric(12.2, 50, 54.0, 10, 70)
        rig.run(low_lv, frame, 5)
        rig.run(BOOST, frame, 2)
        rig.run(low_lv, frame, 3)
        rig.run(fault, frame, 4)
        rig.run(IDLE, frame, 1)
        v = rig.totals.values
        self.assertEqual(v['lv_low_alarms'], 2)
        self.assertEqual(rig.history('LowStarterVoltageAlarms'), 2)
        self.assertEqual(rig.history('LowVoltageAlarms'), 0)
        self.assertEqual(v['fault_events'], 1)
        self.assertEqual(v['last_fault'], 'Product overheating')

    def test_temperature_warnings_and_alarms(self):
        rig = Rig()
        frame = electric(13.0, 50, 54.0, 10, 101)
        rig.run(BOOST, frame, 2, level=driver.TEMP_WARNING)
        rig.run(BOOST, frame, 2, level=driver.TEMP_ALARM)
        rig.run(BOOST, frame, 2, level=driver.TEMP_OK)
        rig.run(BOOST, frame, 2, level=driver.TEMP_ALARM)
        v = rig.totals.values
        self.assertEqual((v['temperature_warnings'], v['temperature_alarms']), (1, 2))

    def test_kept_across_a_restart(self):
        rig = Rig(self.path)
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 600)
        rig.totals.save()
        with open(self.path) as f:
            self.assertGreater(json.load(f)['hv_charged'], 200)
        again = Rig(self.path)
        again.d.refresh(again.t.snapshot())
        self.assertAlmostEqual(again.history('EnergyOut'), 0.27, places=2)
        self.assertEqual(again.history('MaximumTemperature'), 80)

    def test_saved_now_and_then_not_every_second(self):
        rig = Rig(self.path)
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 60)
        self.assertFalse(os.path.exists(self.path))
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), driver.TOTALS_SAVE_INTERVAL)
        self.assertTrue(os.path.exists(self.path))

    def test_a_damaged_file_starts_fresh(self):
        with open(self.path, 'w') as f:
            f.write('{"hv_charged": "lots", "temperature_max": 90')
        totals = driver.Totals(self.path)
        self.assertEqual(totals.values['hv_charged'], 0)
        self.assertIsNone(totals.values['temperature_max'])

    def test_clear_history_button(self):
        rig = Rig(self.path)
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 60)
        svc = rig.d.svc
        self.assertEqual(svc['/History/CanBeCleared'], 1)
        self.assertEqual(svc.SetValue('/History/Clear', 1), 0)
        self.assertEqual(rig.history('EnergyOut'), 0)
        self.assertIsNone(rig.history('MaximumTemperature'))
        with open(self.path) as f:
            self.assertEqual(json.load(f)['hv_charged'], 0)
        rig.d.refresh(rig.t.snapshot())
        self.assertIsNone(svc['/History/Clear'])   # ready for the next press
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 60)
        self.assertEqual(svc.SetValue('/History/Clear', 1), 0)   # a second press works too
        self.assertEqual(rig.history('EnergyOut'), 0)

    def test_dcdc_class_puts_the_12v_side_first(self):
        cfg = load_config()
        cfg['dbus']['service_class'] = 'dcdc'
        totals = driver.Totals()
        d = driver.DbusService(cfg, driver.UiSettings(os.path.join(self.tmp, 'ui.json')), totals=totals)
        totals.values.update(lv_voltage_min=12.5, hv_voltage_min=52.0, lv_charged=500.0)
        d.refresh(driver.Telemetry(1).snapshot())
        self.assertEqual(d.svc['/History/MinimumVoltage'], 12.5)
        self.assertEqual(d.svc['/History/MinimumStarterVoltage'], 52.0)
        self.assertEqual(d.svc['/History/EnergyOut'], 0.5)

    def test_on_the_web_page(self):
        rig = Rig()
        rig.run(BOOST, electric(13.0, 132, 54.0, 30, 80), 3600)
        app = driver.WebApp(load_config(), rig.t, driver.History(rig.t), driver.TemperatureUnit('fahrenheit'),
                            totals=rig.totals)
        t = app.status()['totals']
        self.assertAlmostEqual(t['hv_charged_kwh'], 1.62, places=2)
        self.assertAlmostEqual(t['boost_hours'], 1.0, places=2)
        self.assertIn('since', t)


if __name__ == '__main__':
    unittest.main()
