"""Tests for "find the converter" (``--scan``, Cerbo Setup option 2).

The scan listens on every CAN port of the Cerbo. These tests fake the ports with a temporary
folder that looks like Linux's /sys/class/net, and a fake bus that either hears the D1, hears
nothing, or sees bus errors (what a D1 at a different bitrate looks like).
"""

import contextlib
import io
import os
import tempfile
import unittest

from support import driver, nc


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for name, kind in (('can0', '280'), ('can1', '280'), ('eth0', '1')):
            os.makedirs(os.path.join(self.root, name, 'statistics'))
            self.write(name, 'type', kind)
            self.write(name, 'operstate', 'up')
            self.write(name, 'statistics/rx_errors', '0')
        self.version_file = os.path.join(self.root, 'version')
        with open(self.version_file, 'w') as f:
            f.write('v3.80\n20260901\n')
        self.scenario = {}
        root, scenario, write = self.root, self.scenario, self.write

        class FakeBus:
            def __init__(self, name):
                self.name, self.errors = name, 0

            def recv(self):
                driver.time.sleep(0.01)
                if scenario.get(self.name) == 'ok':
                    return nc.ID_ELECTRIC, bytes(8)
                if scenario.get(self.name) == 'wrong-speed':
                    # A D1 at the other bitrate causes hundreds of bus errors a second. Add enough
                    # per call that even one call clears the threshold, however slow the machine.
                    self.errors += 25
                    write(self.name, 'statistics/rx_errors', str(self.errors))
                return None

            def close(self):
                pass

        self._saved = (driver.SYS_NET, driver.VERSION_FILE, driver.CanBus, driver.can_link_info)
        driver.SYS_NET, driver.VERSION_FILE, driver.CanBus = root, self.version_file, FakeBus
        driver.can_link_info = lambda name: (250000, 'ERROR-ACTIVE')

    def tearDown(self):
        driver.SYS_NET, driver.VERSION_FILE, driver.CanBus, driver.can_link_info = self._saved

    def write(self, name, leaf, text):
        with open(os.path.join(self.root, name, leaf), 'w') as f:
            f.write(text + '\n')

    def scan(self, configured):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            driver.run_scan(configured, seconds=0.2)
        return out.getvalue()

    def test_finds_the_converter_and_suggests_the_port(self):
        self.scenario['can1'] = 'ok'
        out = self.scan('can0')
        self.assertIn('can1    250 kbit/s', out)
        self.assertIn('CONVERTER FOUND', out)
        self.assertIn('interface = can1', out)

    def test_spots_a_wrong_bitrate(self):
        self.scenario['can1'] = 'wrong-speed'
        out = self.scan('can1')
        self.assertIn('bus errors', out)
        self.assertIn('CAN-bus BMS LV (500 kbit/s)', out)

    def test_warns_about_old_firmware_and_missing_converter(self):
        with open(self.version_file, 'w') as f:
            f.write('v3.70\n')
        out = self.scan('can1')
        self.assertIn('need v3.80', out)
        self.assertIn('KL30 and KL15', out)

    def test_works_without_iproute2(self):
        driver.can_link_info = lambda name: (None, None)
        self.scenario['can1'] = 'ok'
        out = self.scan('can1')
        self.assertIn('speed ?', out)
        self.assertIn('matches config.ini', out)

    def test_real_helper_fails_gracefully_off_the_cerbo(self):
        self.assertEqual(self._saved[3]('no-such-port'), (None, None))


if __name__ == '__main__':
    unittest.main()
