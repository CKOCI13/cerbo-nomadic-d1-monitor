"""Direction of the currents, and where they appear on the GX (alternator or DC-DC converter).

First test on real hardware (Cerbo GX, Venus OS v3.85, D1 boosting from a running engine): the D1
reported 13.0 V / 132 A on the 12 V side and 54.2 V / 30 A on the 48 V side, both currents positive,
while its operating mode said "Boost 12V to 48V" and the Lithionics battery showed +26 A charging.
"""

import os
import tempfile
import unittest

from support import driver, nc, load_config


def telemetry(mode, lv_v, lv_i, hv_v, hv_i, temperature=44):
    t = driver.Telemetry(60)
    t.feed(nc.ID_PARAMETER, nc.encode_parameter(operating_mode=mode))
    t.feed(nc.ID_ELECTRIC, nc.encode_electric(lv_v, lv_i, hv_v, hv_i, temperature))
    return t


REAL_BOOST = (driver.MODE_BOOST, 13.0, 132, 54.2, 30)


class OrientTest(unittest.TestCase):
    def test_boost_as_a_real_d1_reports_it(self):
        e = telemetry(*REAL_BOOST).snapshot()['electric']
        self.assertEqual((e['lv_current'], e['hv_current']), (-132, 30))   # out of 12 V, into 48 V
        self.assertEqual((e['lv_current_reported'], e['hv_current_reported']), (132, 30))
        self.assertEqual((driver.power(e, 'lv'), driver.power(e, 'hv')), (-1716, 1626))

    def test_buck(self):
        for hv_i in (40, -40):   # whichever sign the D1 uses
            e = telemetry(driver.MODE_BUCK, 13.6, 150, 53.0, hv_i).snapshot()['electric']
            self.assertEqual((e['lv_current'], e['hv_current']), (150, -40))

    def test_other_modes_keep_the_reported_sign(self):
        e = telemetry(0x2, 13.2, -1, 53.0, 1).snapshot()['electric']   # standby
        self.assertEqual((e['lv_current'], e['hv_current']), (-1, 1))

    def test_history_averages_use_the_same_direction(self):
        t = telemetry(*REAL_BOOST)
        self.assertEqual(t.take_averages()['lv_current'], -132)


class GxMappingTest(unittest.TestCase):
    def service(self, service_class):
        cfg = load_config()
        cfg['dbus']['service_class'] = service_class
        d = driver.DbusService(cfg, driver.UiSettings(os.path.join(tempfile.mkdtemp(), 'ui.json')))
        d.refresh(telemetry(*REAL_BOOST).snapshot())
        return d.svc

    def test_alternator_is_the_default(self):
        self.assertEqual(load_config()['dbus']['service_class'], 'alternator')

    def test_alternator_boosting_charges_the_48v_battery(self):
        s = self.service('alternator')
        self.assertEqual(s.name, 'com.victronenergy.alternator.nomadic_auto')
        # Output = the 48 V battery it charges: positive, so the Overview tile and the GX's
        # DC loads figure see it as charging power.
        self.assertEqual((s['/Dc/0/Voltage'], s['/Dc/0/Current'], s['/Dc/0/Power']), (54.2, 30, 1626))
        # Input = taken from the 12 V side
        self.assertEqual((s['/Dc/In/V'], s['/Dc/In/I'], s['/Dc/In/P']), (13.0, 132, 1716))
        self.assertEqual(s['/Dc/0/Temperature'], 44)   # shown on the Overview tile

    def test_dcdc_boosting_runs_backwards(self):
        s = self.service('dcdc')
        self.assertEqual(s.name, 'com.victronenergy.dcdc.nomadic_auto')
        # Output = the 12 V side, input = the 48 V side; boosting, both run the other way
        self.assertEqual((s['/Dc/0/Voltage'], s['/Dc/0/Current'], s['/Dc/0/Power']), (13.0, -132, -1716))
        self.assertEqual((s['/Dc/In/V'], s['/Dc/In/I'], s['/Dc/In/P']), (54.2, -30, -1626))
        # A dcdc device page would label it "Battery temperature", so it isn't published there
        self.assertNotIn('/Dc/0/Temperature', s.paths)

    def test_switch_pane_cards_keep_both_sides(self):
        s = self.service('alternator')
        self.assertEqual(s['/GenericInput/lv_current/Value'], -132)
        self.assertEqual(s['/GenericInput/hv_power/Value'], 1626)


if __name__ == '__main__':
    unittest.main()
