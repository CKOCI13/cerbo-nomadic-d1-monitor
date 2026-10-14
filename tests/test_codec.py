"""Tests for the CAN encoder/decoder (dbus-nomadic-dcdc/nomadic_can.py).

The byte strings below were worked out by hand from the bit tables in Nomadic's CAN interface
document (NI-TLN-CONTROLS-DCDCCAN-R1), independently of the code, so these tests check the code
against the specification rather than against itself.
"""

import unittest

import support  # noqa: F401  (puts the driver folder on the import path)
import nomadic_can as nc


class MonitorOnlyTest(unittest.TestCase):
    def test_there_is_no_way_to_build_a_control_frame(self):
        # The control message (EMS_DCDC_CTRL, 0x018) can't be built, and isn't even listened for.
        for name in ('encode_ctrl', 'decode_ctrl', 'ID_CTRL', 'CTRL_DIRECTION'):
            self.assertFalse(hasattr(nc, name), name)
        self.assertEqual(nc.RX_IDS, (0x066, 0x088, 0x520))


class ParameterTest(unittest.TestCase):
    def test_decode_known_frame(self):
        # LV overcurrent, derating, high LV voltage, CAN loss + sensor fault,
        # buck mode, precharge not in process
        p = nc.decode_parameter(bytes.fromhex('06 04 82 24 00 00 00 00'))
        self.assertEqual(p['load_status'], 0b10)
        self.assertEqual(p['fault_status'], 0b001)
        self.assertEqual(p['voltage_status'], 0x04)
        self.assertEqual(p['faults'], 0x208)
        self.assertEqual(p['operating_mode'], 4)
        self.assertEqual(p['precharge_status'], 2)
        self.assertEqual(nc.alarm_texts(p), [
            'CAN loss',
            'Sensor fault',
            'Overcurrent on LV network',
            'High voltage on LV network',
            'Power limitation active (derating)',
        ])

    def test_alarm_table_covers_every_bit_once(self):
        widths = {'fault_status': 3, 'faults': 10, 'load_status': 2, 'voltage_status': 8}
        self.assertEqual(sorted((f, b) for f, b, _ in nc.ALARMS),
                         sorted((f, b) for f, w in widths.items() for b in range(w)))
        faults = nc.ALARMS[:nc.FAULT_ALARM_COUNT]
        self.assertTrue(all(f in ('fault_status', 'faults', 'load_status') for f, _, _ in faults))
        self.assertEqual(nc.ALARMS[nc.FAULT_ALARM_COUNT][0], 'voltage_status')

    def test_all_clear(self):
        p = nc.decode_parameter(bytes.fromhex('00 00 00 02 00 00 00 00'))
        self.assertEqual(p['operating_mode_text'], 'Idle')
        self.assertEqual(nc.alarm_texts(p), [])


class ElectricTest(unittest.TestCase):
    def test_decode_known_frame(self):
        # LV 13.6 V / +20 A, HV 51.2 V / -5 A, 35 degC
        e = nc.decode_electric(bytes.fromhex('44 44 E0 07 D0 53 00 00'))
        self.assertEqual(e, {
            'lv_voltage': 13.6,
            'lv_current': 20,
            'hv_voltage': 51.2,
            'hv_current': -5,
            'temperature': 35,
        })

    def test_extremes(self):
        e = nc.decode_electric(bytes(8))
        self.assertEqual((e['lv_current'], e['hv_current'], e['temperature']), (-255, -255, -48))
        e = nc.decode_electric(bytes.fromhex('FF FF FF FF F8 FF 00 00'))
        self.assertEqual(e, {
            'lv_voltage': 51.1,
            'lv_current': 256,
            'hv_voltage': 102.3,
            'hv_current': 256,
            'temperature': 207,
        })


class EncodeTest(unittest.TestCase):
    """The simulation encoders must produce exactly the hand-worked frames above."""

    def test_electric(self):
        self.assertEqual(nc.encode_electric(13.6, 20, 51.2, -5, 35), bytes.fromhex('44 44 E0 07 D0 53 00 00'))

    def test_parameter(self):
        frame = nc.encode_parameter(load_status=0b10, fault_status=0b001, voltage_status=0x04,
                                    faults=0x208, operating_mode=4, precharge_status=2)
        self.assertEqual(frame, bytes.fromhex('06 04 82 24 00 00 00 00'))

    def test_version(self):
        self.assertEqual(nc.encode_version(0x123, 5), bytes.fromhex('12 35 00 00 00 00 00 00'))

    def test_clamps_out_of_range(self):
        e = nc.decode_electric(nc.encode_electric(99, 999, -3, -999, 500))
        self.assertEqual((e['lv_voltage'], e['lv_current'], e['hv_voltage'], e['hv_current'], e['temperature']),
                         (51.1, 256, 0.0, -255, 207))


class VersionTest(unittest.TestCase):
    def test_decode_known_frame(self):
        v = nc.decode_version(bytes.fromhex('12 35 00 00 00 00 00 00'))
        self.assertEqual(v, {'software_raw': 0x123, 'software': '1.2.3', 'hardware': 5})


if __name__ == '__main__':
    unittest.main()
