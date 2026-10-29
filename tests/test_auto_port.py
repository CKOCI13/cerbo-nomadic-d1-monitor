"""Tests for interface = auto: the driver finds the converter's CAN port by itself.

Port names differ between Cerbo models (can0/can1 on the original, vecan0/vecan1 on the MK2), and a
USB-stick install has no way to run "find the converter", so auto is the default. These tests fake
the ports with a folder that looks like /sys/class/net and a fake bus per port.
"""

import os
import tempfile
import unittest

from support import driver, nc, load_config

MONITOR = not hasattr(driver, 'Controller')
CONVERTER = (nc.ID_ELECTRIC, nc.ID_PARAMETER, nc.ID_ELECTRIC, nc.ID_VERSION)   # what the converter sends


class AutoPortTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for name, kind in (('eth0', '1'), ('vecan0', '280'), ('vecan1', '280')):
            os.makedirs(os.path.join(self.root, name))
            with open(os.path.join(self.root, name, 'type'), 'w') as f:
                f.write(kind + '\n')
        self.traffic = {}     # port -> CAN IDs heard there (in turn), or (IDs, frame length)
        self.sent = []        # (port, can_id) of every transmitted frame
        self.opened = []      # ports a bus was opened on
        traffic, sent, opened = self.traffic, self.sent, self.opened

        class FakeBus:
            def __init__(self, name):
                self.interface = name
                opened.append(name)

            def recv(self):
                driver.time.sleep(0.002)
                heard = traffic.get(self.interface)
                if heard is None:
                    return None
                ids, length = heard if isinstance(heard[-1], int) and len(heard) == 2 and \
                    isinstance(heard[0], tuple) else (heard, 8)
                self.n = getattr(self, 'n', -1) + 1
                return ids[self.n % len(ids)], bytes(length)

            def open_tx(self):
                pass

            def send(self, can_id, data):
                sent.append((self.interface, can_id))

            def close(self):
                pass

        self._saved = (driver.SYS_NET, driver.CanBus)
        driver.SYS_NET, driver.CanBus = self.root, FakeBus

    def tearDown(self):
        driver.SYS_NET, driver.CanBus = self._saved

    def make_bus(self, cfg):
        return driver.AutoCanBus(cfg) if MONITOR else driver.AutoCanBus(cfg, transmit=True)

    def test_auto_is_the_default(self):
        self.assertEqual(driver.DEFAULTS['can']['interface'], 'auto')
        self.assertEqual(load_config()['can']['interface'], 'auto')

    def test_lists_only_can_ports(self):
        self.assertEqual(driver.can_ports(), ['vecan0', 'vecan1'])

    def test_finds_the_port_the_converter_is_on(self):
        self.traffic['vecan1'] = CONVERTER
        self.assertEqual(driver.find_converter(seconds=0.2), 'vecan1')

    def test_ignores_other_traffic(self):
        # A battery BMS on the other port uses other IDs (e.g. 0x351): that is not the converter.
        self.traffic['vecan0'] = (0x351,)
        self.assertIsNone(driver.find_converter(seconds=0.1))

    def test_one_matching_id_is_not_enough(self):
        # Something else that happens to use one of the converter's IDs must not be taken for it.
        self.traffic['vecan0'] = (nc.ID_ELECTRIC,)
        self.assertIsNone(driver.find_converter(seconds=0.1))

    def test_frames_of_the_wrong_length_are_not_the_converter(self):
        self.traffic['vecan0'] = (CONVERTER, 4)
        self.assertIsNone(driver.find_converter(seconds=0.1))

    def test_uses_the_found_port_and_reports_it(self):
        self.traffic['vecan0'] = (0x351,)
        self.traffic['vecan1'] = CONVERTER
        cfg = load_config()
        bus = self.make_bus(cfg)
        self.assertEqual(bus.interface, 'auto')
        self.assertIn(bus.recv(), [(i, bytes(8)) for i in CONVERTER])
        self.assertEqual(bus.interface, 'vecan1')
        self.assertEqual(cfg['can']['interface'], 'vecan1')   # web page and device page show it
        watcher = driver.StatusWatcher('auto')
        watcher.bus = bus
        self.assertEqual(watcher.interface, 'vecan1')

    @unittest.skipIf(MONITOR, 'control edition only')
    def test_control_sends_nothing_until_found_and_only_there(self):
        bus = self.make_bus(load_config())
        self.assertIs(bus.send(nc.ID_CTRL, bytes(8)), False)
        self.assertEqual(self.sent, [])
        self.traffic['vecan1'] = CONVERTER
        bus.recv()
        self.assertIs(bus.send(nc.ID_CTRL, bytes(8)), True)
        self.assertEqual(self.sent, [('vecan1', nc.ID_CTRL)])

    @unittest.skipIf(not MONITOR, 'monitor edition only')
    def test_monitor_auto_bus_cannot_send(self):
        self.assertFalse(hasattr(driver.AutoCanBus, 'send'))


if __name__ == '__main__':
    unittest.main()
