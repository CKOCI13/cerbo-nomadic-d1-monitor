# Tests

Automated tests for the monitor. They run on any computer with Python 3: no Cerbo, no converter and no extra
packages needed.

## Running them

From the project folder (the one with `README.md` in it):

```bash
python -m unittest discover -s tests -v
```

On Mac or Linux, type `python3` instead of `python`. They take a few seconds. GitHub also runs them
automatically on every change; the result is the "tests" badge at the top of the [project README](../README.md).

## What each file checks

| File | What it proves |
|---|---|
| [`test_codec.py`](test_codec.py) | Decoding of the D1's three CAN messages, checked against frames worked out **by hand** from Nomadic's specification. The alarm list covers every status bit exactly once. There is no way to build a control message |
| [`test_monitor_only.py`](test_monitor_only.py) | Nothing in the driver folder can transmit: the CAN class has no send method, no control code is left, and no `.send…` / `.write` call exists apart from the web server's response |
| [`test_telemetry_and_web.py`](test_telemetry_and_web.py) | Readings go stale when the D1 goes quiet. Short frames are ignored. The averages and 24-hour history, with gaps when offline. The web page needs its password, serves status and history, and refuses everything but reading |
| [`test_dbus.py`](test_dbus.py) | Everything shown on the Cerbo and in VRM meets the display rules in Victron's GUI source code. Nothing is writable except display settings. The three cards, readings and power, alarms, "No communication", renaming that survives a restart, turning the switch pane off |
| [`test_scan.py`](test_scan.py) | "Find the converter" finds the right port, spots a wrong bitrate from bus errors, warns about old firmware, and works without `iproute2` |
| [`test_usb_installer.py`](test_usb_installer.py) | The USB-stick installer unpacks to `/data/dbus-nomadic-dcdc`, leaves out run-time files, and its hook runs `install.sh` only after a good unpack (needs `sh` and `tar`, so it is skipped on plain Windows) |
| [`test_simulation.py`](test_simulation.py) | The pretend D1 follows the manual's automatic logic (buck, standby, boost). The whole monitor runs end to end against it |

## How they run without a Cerbo

- [`support.py`](support.py) loads the driver by its file path (its name contains dashes, so a normal `import`
  doesn't work) and puts the driver folder on the import path.
- [`fakes/vedbus.py`](fakes/vedbus.py) stands in for Victron's `vedbus` library, which needs the Linux D-Bus.
  It keeps the same rules: paths are added before registering, and outside writes go through the driver's
  checks with velib's return codes.
- Nothing here opens a real CAN socket. The scan tests use a temporary folder that looks like `/sys/class/net`,
  and the simulation tests use the pretend D1.
