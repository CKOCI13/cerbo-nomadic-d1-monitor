"""Tests for the pretend D1 used by ``--simulate`` (Cerbo Setup option 9).

The pretend D1 lets people try every screen with no converter connected. These tests check that
it behaves like the D1 manual's automatic logic, and that the whole monitor works against it end
to end.
"""

import time
import unittest

from support import driver, nc, load_config


def run(sim, t, seconds, dt=0.05):
    """Advance the model by `seconds` of pretend time."""
    end = t + seconds
    while t < end:
        t += dt
        sim.step(t)
    return t


class PretendD1Test(unittest.TestCase):
    def setUp(self):
        self.sim = driver.SimulatedD1()
        self.t = self.sim._last_step

    def test_automatic_logic_follows_the_manual(self):
        sim = self.sim
        t = run(sim, self.t, 5)
        self.assertEqual(sim.mode, 0x2)            # standby while the 12 V side drains
        self.assertTrue(12.5 < sim.lv < 12.6)
        t = run(sim, t, 15)
        self.assertEqual(sim.state, 'buck')        # below 12.5 V: autonomous buck
        self.assertEqual(sim.mode, 0x5)
        self.assertGreater(sim.lv_i, 0)
        self.assertLess(sim.hv_i, 0)               # the 48 V side is the source
        t = run(sim, t, 140)
        self.assertAlmostEqual(sim.lv, 13.4, delta=0.05)   # regulates to about 13.4 V
        t = run(sim, t, 30)                        # the pretend alternator starts charging
        self.assertEqual(sim.state, 'boost')       # above 13.5 V: boost
        self.assertLess(sim.lv_i, 0)
        self.assertGreater(sim.hv_i, 0)
        for _ in range(20000):                     # boost until the 12 V side is below 12.3 V
            t = run(sim, t, 0.05)
            if sim.state != 'boost':
                break
        self.assertTrue(12.25 <= sim.lv < 12.3)

    def test_frames_decode(self):
        e = nc.decode_electric(self.sim._frame(nc.ID_ELECTRIC))
        self.assertEqual(e['lv_voltage'], 12.6)
        p = nc.decode_parameter(self.sim._frame(nc.ID_PARAMETER))
        self.assertEqual(p['operating_mode_text'], 'Idle')
        self.assertEqual(nc.decode_version(self.sim._frame(nc.ID_VERSION))['software'], '1.0.0')


class EndToEndTest(unittest.TestCase):
    def test_monitor_threads_against_the_pretend_d1(self):
        cfg = load_config()
        driver.simulation_config(cfg)
        self.assertEqual(cfg['dbus']['custom_name'], 'DC-DC D1 (SIMULATED)')
        self.assertEqual(cfg['can']['interface'], 'sim')
        self.assertEqual(cfg['web']['port'], '8089')
        telemetry, bus = driver.Telemetry(1.0), driver.SimulatedD1()
        history = driver.History(telemetry, interval=0.2)
        driver.start_thread('rx', driver.rx_worker, bus, telemetry)
        time.sleep(0.6)
        history.tick()
        s = telemetry.snapshot()
        self.assertTrue(s['online'])
        self.assertGreater(s['counts'][nc.ID_ELECTRIC], 30)
        self.assertEqual(len(history.points()), 1)
        self.assertEqual(history.points()[0][1], 12.6)


if __name__ == '__main__':
    unittest.main()
