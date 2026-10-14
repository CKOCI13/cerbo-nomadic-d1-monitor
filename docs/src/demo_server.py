"""Runs the monitor's real web page against the pretend D1, with an hour of history already filled in.

Used by build.mjs to take the web-page screenshot; also handy for working on the page:

    python docs/src/demo_server.py 8090      then open http://localhost:8090
"""

import importlib.util
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRIVER_DIR = os.path.join(REPO, 'dbus-nomadic-dcdc')
sys.path.insert(0, DRIVER_DIR)
spec = importlib.util.spec_from_file_location('driver', os.path.join(DRIVER_DIR, 'dbus-nomadic-dcdc.py'))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
import nomadic_can as nc  # noqa: E402


def prefill(history, seconds=3600, step=0.05):
    """Fast-forward a pretend D1 through `seconds` and record its history as if it had been live."""
    sim = driver.SimulatedD1()
    t0 = sim._last_step
    sim._start = t0 - 120          # start part-way into the load phase
    wall_end = time.time()
    t, sums, n, next_point = t0, None, 0, t0 + history.interval
    while t < t0 + seconds:
        t += step
        sim.step(t)
        e = nc.decode_electric(sim._frame(nc.ID_ELECTRIC))
        sums = {k: (sums or {}).get(k, 0) + e[k] for k in driver.ELECTRIC_FIELDS}
        n += 1
        if t >= next_point:
            history.add({k: v / n for k, v in sums.items()}, wall_end - (t0 + seconds - t))
            sums, n, next_point = None, 0, next_point + history.interval
    return sim


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    cfg = driver.load_config(os.path.join(DRIVER_DIR, 'config.ini'))
    cfg['web']['port'], cfg['web']['bind'], cfg['web']['password'] = str(port), '127.0.0.1', ''
    telemetry = driver.Telemetry(1.0)
    history = driver.History(telemetry)
    sim = prefill(history)
    now = time.monotonic()                      # carry on live from where the fast-forward ended
    sim._start += now - sim._last_step
    sim._last_step = now
    sim._due = dict.fromkeys(sim.PERIODS, now)
    driver.start_thread('can-rx', driver.rx_worker, sim, telemetry)
    driver.start_web(cfg, driver.WebApp(cfg, telemetry, history))
    print('ready on http://127.0.0.1:%d/' % port, flush=True)
    while True:
        time.sleep(1)
        history.tick()


if __name__ == '__main__':
    main()
