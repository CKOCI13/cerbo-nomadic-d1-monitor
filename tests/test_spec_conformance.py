"""Independent check of the driver against the supplied documentation.

Everything here is written from the documents, not from the driver's code:

  * NI-TLN-CONTROLS-DCDCCAN-R1 (Nomadic CAN interface control document, 2026-06): message IDs, every
    signal's start bit, length, factor, offset, error value and bit meanings.
  * D1 / D2 Bidirectional DC-DC Converter Manual, Rev D: mode thresholds, adjustable ranges, limits.
  * Victron's update-data.sh (meta-victronenergy): which file names a USB stick install picks up.

Bits are read with the classic DBC "Motorola" walk (start bit = MSB, then step down within a byte and
on to the next byte), one bit at a time. That is a different method from the driver's, so the two
check each other.
"""

import fnmatch
import os
import random
import re
import unittest

from support import REPO, driver, nc

CONTROL = hasattr(nc, 'encode_ctrl')

# (message ID, signal, start bit, length, factor, offset) -- straight from the CAN interface document
SIGNALS = {
    0x018: [('DCDC_DIRECTION_REQ', 1, 2, 1, 0), ('DCDC_PRECH_REQ', 4, 1, 1, 0),
            ('DCDC_IMAX_REQ', 15, 8, 1, 0), ('DCDC_UREG_REQ', 23, 12, 0.015, 0),
            ('DCDC_UPRECH_REQ', 27, 12, 0.015, 0)],
    0x066: [('DCDC_LOAD_STATUS', 1, 2, 1, 0), ('DCDC_FAULT_STATUS', 4, 3, 1, 0),
            ('DCDC_VOLTAGE_STATUS', 15, 8, 1, 0), ('DCDC_FAULT', 23, 10, 1, 0),
            ('DCDC_OPERATING_MODE', 27, 4, 1, 0), ('DCDC_PRECH_STATUS', 29, 2, 1, 0)],
    0x088: [('DCDC_U14', 7, 9, 0.1, 0), ('DCDC_I14', 14, 9, 1, -255), ('DCDC_UHV', 21, 10, 0.1, 0),
            ('DCDC_IHV', 27, 9, 1, -255), ('DCDC_TEMP', 47, 8, 1, -48)],
    0x520: [('SW_Version', 7, 12, 1, 0), ('HW_Version', 11, 4, 1, 0)],
}
# The driver's names for the decoded fields
FIELDS = {
    'DCDC_LOAD_STATUS': 'load_status', 'DCDC_FAULT_STATUS': 'fault_status',
    'DCDC_VOLTAGE_STATUS': 'voltage_status', 'DCDC_FAULT': 'faults',
    'DCDC_OPERATING_MODE': 'operating_mode', 'DCDC_PRECH_STATUS': 'precharge_status',
    'DCDC_U14': 'lv_voltage', 'DCDC_I14': 'lv_current', 'DCDC_UHV': 'hv_voltage',
    'DCDC_IHV': 'hv_current', 'DCDC_TEMP': 'temperature',
    'SW_Version': 'software_raw', 'HW_Version': 'hardware',
}


def motorola_bits(start, length):
    """Bit positions (byte*8 + bit, bit 7 = MSB of the byte) from MSB to LSB."""
    pos, out = start, []
    for _ in range(length):
        out.append(pos)
        pos = pos + 15 if pos % 8 == 0 else pos - 1
    return out


def put(data, start, length, raw):
    for i, pos in enumerate(motorola_bits(start, length)):
        bit = (raw >> (length - 1 - i)) & 1
        byte, b = divmod(pos, 8)
        data[byte] = (data[byte] & ~(1 << b)) | (bit << b)


def get(data, start, length):
    raw = 0
    for pos in motorola_bits(start, length):
        byte, b = divmod(pos, 8)
        raw = (raw << 1) | ((data[byte] >> b) & 1)
    return raw


class LayoutTest(unittest.TestCase):
    def test_message_ids(self):
        self.assertEqual((nc.ID_PARAMETER, nc.ID_ELECTRIC, nc.ID_VERSION), (0x066, 0x088, 0x520))
        self.assertEqual(set(nc.RX_IDS), {0x066, 0x088, 0x520})
        if CONTROL:
            self.assertEqual(nc.ID_CTRL, 0x018)
            self.assertAlmostEqual(driver.TX_PERIOD, 0.010)   # EMS_DCDC_CTRL period 10 ms

    def test_no_signals_overlap_and_all_fit_in_8_bytes(self):
        for msg, sigs in SIGNALS.items():
            used = set()
            for name, start, length, _, _ in sigs:
                bits = set(motorola_bits(start, length))
                self.assertTrue(all(0 <= b < 64 for b in bits), name)
                self.assertFalse(used & bits, '%s overlaps another signal in 0x%03X' % (name, msg))
                used |= bits


class DecodeAgainstSpecTest(unittest.TestCase):
    """Fill each signal with random raw values by the spec, decode with the driver, compare."""

    DECODERS = {0x066: 'decode_parameter', 0x088: 'decode_electric', 0x520: 'decode_version'}

    def test_every_received_signal(self):
        rng = random.Random(2026)
        for msg, fn in self.DECODERS.items():
            decode = getattr(nc, fn)
            for _ in range(2000):
                data, expected = bytearray(8), {}
                for name, start, length, factor, offset in SIGNALS[msg]:
                    raw = rng.randrange(1 << length)
                    if name == 'DCDC_PRECH_STATUS':
                        raw = rng.randrange(4)
                    put(data, start, length, raw)
                    expected[FIELDS[name]] = raw * factor + offset
                got = decode(bytes(data))
                for field, value in expected.items():
                    self.assertAlmostEqual(got[field], value, places=6,
                                           msg='0x%03X %s' % (msg, field))

    def test_extremes(self):
        e = nc.decode_electric(bytes(8))
        self.assertEqual((e['lv_voltage'], e['lv_current'], e['hv_voltage'], e['hv_current'], e['temperature']),
                         (0, -255, 0, -255, -48))
        e = nc.decode_electric(b'\xff' * 8)
        self.assertEqual((e['lv_voltage'], e['lv_current'], e['hv_voltage'], e['hv_current'], e['temperature']),
                         (51.1, 256, 102.3, 256, 207))   # the spec's ranges: 51.1 V, 256 A, 102.3 V, 207 C

    def test_software_version_is_bcd(self):
        data = bytearray(8)
        put(data, 7, 12, 0x123)
        self.assertEqual(nc.decode_version(bytes(data))['software'], '1.2.3')


class MeaningsTest(unittest.TestCase):
    def test_operating_modes(self):
        spec = {1: 'initiali', 2: 'idle', 3: 'boost', 4: 'buck', 5: 'autonomous', 7: 'shut',
                8: 'derating', 0xF: 'invalid'}
        for code, word in spec.items():
            self.assertIn(word, nc.OPERATING_MODES[code].lower(), code)
        self.assertNotIn(6, nc.OPERATING_MODES)   # "Other values reserved"

    def test_fault_bits_in_spec_order(self):
        spec = ['t15170', '5v', 'precharge', 'can loss', 'buck', 'cvt', 'hardware', 'overheat',
                'power supply', 'sensor']
        self.assertEqual(len(nc.FAULT_BITS), 10)
        for text, word in zip(nc.FAULT_BITS, spec):
            self.assertIn(word, text.lower())

    def test_voltage_and_load_and_fault_status_bits(self):
        spec = ['undervoltage on lv', 'low voltage on lv', 'high voltage on lv', 'overvoltage on lv',
                'undervoltage on hv', 'low voltage on hv', 'high voltage on hv', 'overvoltage on hv']
        for text, words in zip(nc.VOLTAGE_STATUS_BITS, spec):
            self.assertIn(words, text.lower())
        self.assertIn('hv', nc.LOAD_STATUS_BITS[0].lower())
        self.assertIn('lv', nc.LOAD_STATUS_BITS[1].lower())
        for text, word in zip(nc.FAULT_STATUS_BITS, ['limitation', 'temporary', 'permanent']):
            self.assertIn(word, text.lower())

    def test_every_alarm_bit_reaches_the_owner(self):
        for field, bits in (('faults', 10), ('voltage_status', 8), ('load_status', 2), ('fault_status', 3)):
            for bit in range(bits):
                p = dict(load_status=0, fault_status=0, voltage_status=0, faults=0)
                p[field] = 1 << bit
                self.assertEqual(len(nc.alarm_texts(p)), 1, '%s bit %d' % (field, bit))


@unittest.skipUnless(CONTROL, 'monitor edition: sends nothing')
class EncodeAgainstSpecTest(unittest.TestCase):
    def test_control_frame_fields(self):
        rng = random.Random(7)
        for _ in range(2000):
            direction = rng.choice((0, 1, 2))
            prech = rng.choice((0, 1))
            imax = rng.randrange(0, 255)
            ureg = round(rng.uniform(0, 61.4), 3)
            uprech = round(rng.uniform(0, 61.4), 3)
            data = nc.encode_ctrl(direction, prech, imax, ureg, uprech)
            self.assertEqual(len(data), 8)
            s = {n: get(data, st, ln) for n, st, ln, _, _ in SIGNALS[0x018]}
            self.assertEqual(s['DCDC_DIRECTION_REQ'], direction)
            self.assertEqual(s['DCDC_PRECH_REQ'], prech)
            self.assertEqual(s['DCDC_IMAX_REQ'], imax)
            self.assertLessEqual(abs(s['DCDC_UREG_REQ'] * 0.015 - ureg), 0.0075 + 1e-9)
            self.assertLessEqual(abs(s['DCDC_UPRECH_REQ'] * 0.015 - uprech), 0.0075 + 1e-9)
            used = set()
            for _, st, ln, _, _ in SIGNALS[0x018]:
                used |= set(motorola_bits(st, ln))
            for pos in set(range(64)) - used:      # unused bits are sent as 0
                self.assertEqual((data[pos // 8] >> (pos % 8)) & 1, 0)

    def test_error_values_are_never_sent(self):
        with self.assertRaises(ValueError):
            nc.encode_ctrl(1, 0, 255, 13.4, 48)         # 0xFF is the IMAX error value
        with self.assertRaises(ValueError):
            nc.encode_ctrl(1, 0, 10, 61.425, 48)        # 0xFFF is the UREG error value
        with self.assertRaises(ValueError):
            nc.encode_ctrl(3, 0, 10, 13.4, 48)          # 3 = "Invalid signal"


class ManualTest(unittest.TestCase):
    """Rev D manual: adjustable ranges, limits and the automatic mode thresholds."""

    def setUp(self):
        from support import load_config
        self.cfg = load_config()

    @unittest.skipUnless(CONTROL, 'monitor edition has no limits')
    def test_shipped_limits_stay_inside_the_manuals_ranges(self):
        c = self.cfg['control']
        self.assertGreaterEqual(c.getfloat('buck_voltage_min'), 10.0)
        self.assertLessEqual(c.getfloat('buck_voltage_max'), 13.5)
        self.assertGreaterEqual(c.getfloat('boost_voltage_min'), 42.0)
        self.assertLessEqual(c.getfloat('boost_voltage_max'), 57.8)
        self.assertLessEqual(c.getint('buck_current_limit_max'), 178)
        self.assertLessEqual(c.getint('boost_current_limit_max'), 57)
        # Stock LTV1 wiring (2 AWG, 150 A fuse) kept to 80 %: 120 A on the 12 V cable either way
        self.assertLessEqual(c.getint('buck_current_limit_max'), 120)
        worst_12v = c.getint('boost_current_limit_max') * 58.0 / (12.3 * 0.92)
        self.assertLessEqual(worst_12v, 120)

    @unittest.skipUnless(CONTROL, 'monitor edition never decides anything')
    def test_automatic_thresholds_are_the_manuals(self):
        a = driver.AutoLogic
        self.assertEqual((a.BUCK_BELOW, a.HV_MIN, a.HV_MAX), (12.5, 41.0, 58.0))
        self.assertEqual((a.BUCK_EXIT_ABOVE, a.BOOST_FROM, a.LV_MAX, a.BOOST_STOP_BELOW), (13.5, 13.5, 16.0, 12.3))
        self.assertAlmostEqual(a.BUCK_TARGET_MAX, 13.4)   # "~13.4 V regulated" buck output

    def test_simulator_follows_the_manual(self):
        sim = next(getattr(driver, n) for n in dir(driver) if re.fullmatch(r'SimulatedD\d', n))
        try:
            s = sim(engine=False)
        except TypeError:
            s = sim()
        if hasattr(s, 'engine'):
            s.engine = False
        t = s._last_step
        s.lv, s.hv = 12.4, 51.0                    # below 12.5 V with the 48 V side in 41-58 V
        for _ in range(10):
            t += 0.05
            s.step(t)
        self.assertGreater(s.lv_i, 0)              # it bucks into the 12 V side
        s2 = sim(engine=False) if 'engine' in sim.__init__.__code__.co_varnames else sim()
        if hasattr(s2, 'engine'):
            s2.engine = False
        t = s2._last_step
        s2.lv, s2.hv = 13.8, 51.0                  # 12 V side 13.5-16 V: boost
        for _ in range(3):
            t += 0.05
            s2.step(t)
        self.assertLess(s2.lv_i, 0)                # it takes energy from the 12 V side
        self.assertGreater(s2.hv_i, 0)             # and pushes it into the 48 V side
        # (The 41-58 V buck window is checked in test_auto_logic; the pretend 48 V battery
        # never leaves 44-57.5 V, so it can't be shown here.)


class VictronInstallTest(unittest.TestCase):
    def test_usb_file_name_is_one_venus_os_unpacks(self):
        script = open(os.path.join(REPO, 'make-usb-installer.sh')).read()
        name = re.search(r'NAME=(\S+)', script).group(1)
        # update-data.sh: ls $dir/venus-data-*.tar.gz (word-split, so no spaces allowed)
        self.assertTrue(fnmatch.fnmatch(name, 'venus-data-*.tar.gz'), name)
        self.assertNotIn(' ', name)
        hook = open(os.path.join(REPO, 'usb', 'rc', 'post-hook.sh')).read()
        self.assertIn(name, hook)                  # the hook renames this exact file after installing


if __name__ == '__main__':
    unittest.main()
