"""Tests for the readings (Telemetry), their history, and the built-in web page's API."""

import base64
import json
import time
import unittest
import urllib.error
import urllib.request

from support import driver, nc, load_config, free_port, ELECTRIC_FRAME, PARAMETER_FRAME, VERSION_FRAME


class TelemetryTest(unittest.TestCase):
    def test_readings_go_stale_but_the_version_is_kept(self):
        t = driver.Telemetry(0.2)
        self.assertFalse(t.snapshot()['online'])
        t.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)
        t.feed(nc.ID_PARAMETER, PARAMETER_FRAME)
        t.feed(nc.ID_VERSION, VERSION_FRAME)
        s = t.snapshot()
        self.assertTrue(s['online'])
        self.assertEqual(s['electric']['lv_voltage'], 13.6)
        time.sleep(0.25)
        s = t.snapshot()
        self.assertFalse(s['online'])
        self.assertIsNone(s['electric'])
        self.assertEqual(s['version']['software'], '1.2.3')

    def test_short_frames_are_ignored(self):
        t = driver.Telemetry(1)
        t.feed(nc.ID_ELECTRIC, bytes(4))
        self.assertEqual(t.bad_frames, 1)
        self.assertFalse(t.snapshot()['online'])

    def test_averages_since_the_last_call(self):
        t = driver.Telemetry(1)
        self.assertIsNone(t.take_averages())
        t.feed(nc.ID_ELECTRIC, nc.encode_electric(13.0, 10, 50.0, -2, 30))
        t.feed(nc.ID_ELECTRIC, nc.encode_electric(14.0, 30, 52.0, -4, 32))
        a = t.take_averages()
        self.assertAlmostEqual(a['lv_voltage'], 13.5)
        self.assertEqual((a['lv_current'], a['hv_current'], a['temperature']), (20, -3, 31))
        self.assertIsNone(t.take_averages())   # reset after each call


class HistoryTest(unittest.TestCase):
    def test_one_averaged_point_per_interval_with_gaps_when_offline(self):
        t = driver.Telemetry(1)
        h = driver.History(t, interval=10, size=3)
        now = time.monotonic()
        h.tick(now)                                 # not due yet
        self.assertEqual(h.points(), [])
        t.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)      # 13.6 V 20 A, 51.2 V -5 A, 35 C
        h.tick(now + 10)
        h.tick(now + 11)                            # same interval: nothing added
        h.tick(now + 20)                            # nothing received: a gap
        self.assertEqual(len(h.points()), 2)
        self.assertEqual(h.points()[0][1:], (13.6, 20.0, 272, 51.2, -5.0, -256, 35.0))
        self.assertEqual(h.points()[1][1:], (None,) * 7)
        for i in range(5):
            h.add(None, wall_time=1000 + i * 10)
        self.assertEqual([p[0] for p in h.points()], [1020, 1030, 1040])   # oldest dropped
        self.assertEqual([p[0] for p in h.points(15)], [1030, 1040])


class WebApiTest(unittest.TestCase):
    """Starts the real web server on a free local port and talks to it like the page does."""

    @classmethod
    def setUpClass(cls):
        cfg = load_config()
        cfg['web']['port'] = str(free_port())
        cfg['web']['bind'] = '127.0.0.1'
        cfg['web']['password'] = 'pw'
        telemetry = driver.Telemetry(60)
        telemetry.feed(nc.ID_ELECTRIC, ELECTRIC_FRAME)
        telemetry.feed(nc.ID_PARAMETER, PARAMETER_FRAME)
        history = driver.History(telemetry)
        history.add(telemetry.take_averages(), wall_time=1000)
        driver.start_web(cfg, driver.WebApp(cfg, telemetry, history))
        cls.base = 'http://127.0.0.1:%s/' % cfg['web']['port']
        cls.auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:pw').decode()}
        time.sleep(0.2)

    def request(self, path, body=None, headers=None, method=None):
        h = dict(headers if headers is not None else self.auth)
        if body is not None:
            h['Content-Type'] = 'application/json'
        r = urllib.request.Request(self.base + path, data=None if body is None else body.encode(),
                                   headers=h, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_password_is_required(self):
        self.assertEqual(self.request('', headers={})[0], 401)
        self.assertEqual(self.request('api/status', headers={})[0], 401)

    def test_page_and_status(self):
        code, page = self.request('')
        self.assertEqual(code, 200)
        self.assertIn(b'monitor only', page)
        self.assertNotIn(b'api/control', page)
        code, body = self.request('api/status')
        st = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(st['online'])
        self.assertTrue(st['monitor_only'])
        self.assertEqual(st['power'], {'lv': 272, 'hv': -256})
        self.assertEqual(st['alarms'][0], 'CAN loss')   # most serious first
        self.assertNotIn('control', st)

    def test_history(self):
        code, body = self.request('api/history?minutes=10')
        h = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(h['fields'][0], 'time')
        self.assertEqual(h['points'][0][:2], [1000, 13.6])

    def test_everything_but_reading_is_refused(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                code, body = self.request('api/control', '{"direction": 1}', method=method)
                self.assertEqual(code, 405)
                self.assertIn(b'read-only', body)
        self.assertEqual(self.request('api/control')[0], 404)


if __name__ == '__main__':
    unittest.main()
