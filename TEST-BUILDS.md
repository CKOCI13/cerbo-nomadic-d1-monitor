# D1 monitor test builds (November 2026)

Three monitor-only builds for bench and field testing. None of them is merged into `main` or published as a
release. The current release is still **2.5.0**. All three only listen to the D1 and never send it anything.

| Label | Version | Branch | What it changes on top of 2.5.0 |
|---|---|---|---|
| **A: Smoothing** | 2.6.0 | [`feature/smoothing-2.6.0`](../../tree/feature/smoothing-2.6.0) (PR #17) | Averages displayed voltage, current and power over 5 s |
| **B: Idle rule** | 2.5.1-idle | [`test/idle-2a`](../../tree/test/idle-2a) | Shows 2 A or less on the 48 V side as Idle, 0 A, 0 W |
| **C: Smoothing + idle** | 2.6.1-test | [`test/smoothing-plus-idle-2.6.1`](../../tree/test/smoothing-plus-idle-2.6.1) | A and B together |

The version shows on the web page (`http://<cerbo-ip>:8088`, Details › Driver) and in the Cerbo's device page.

## What we found in the field (2026-11-14 and 2026-11-16)

Grand Design LTV1, Cerbo GX MK2, D1 alone on VE.Can 2 at 500 kbit/s, Lithionics 51 V battery.

1. **Whole-amp current steps.** The D1 reports current in 1 A steps (per the CAN document). While boosting about
   2 kW, a steady 36.4 A flips between 36 and 37 A, so power jumps about 55 W at 54 V. The Cerbo's DC Loads figure
   (alternator minus battery) then swings between about −58 and +55 W. Build A addresses this.
2. **Fixed offset while parked.** After a drive, with the engine off and the Egis relay still closed, the D1 stays
   in **"Boost 12V to 48V"** and keeps reporting **−5/−6 A at 12.6 V and +2 A at 58.0 V** for hours. That gives a
   steady phantom **116 W** on the Overview, DC Loads, History and VRM. At the same time:
   - the Lithionics BMS reads **0.0 A / 0.0 W** at 100 % (it resolves 0.1 A);
   - a Hall-effect gauge on the chassis-battery cable reads only the 12 V house loads;
   - the D1's figures don't change when loads or the Lithionics are switched on or off;
   - the D1 reports more power out (116 W) than in (63–76 W).

   The readings return to normal when the chassis batteries drop to the Egis open point (about 12.5 V) and the D1
   starts bucking. Build B addresses this on the display side.

**Questions for Nomadic:** Is the parked Boost state with a fixed −5/−6 A / +2 A report expected D1 behaviour? Is
there a current-sensor zero offset at low load, and is there a mode or frame that signals "no power transfer"?

## The idle rule (B and C)

- When the D1 reports **2 A or less on the 48 V side**, both currents are shown as 0 A and 0 W, and the mode as
  **"Idle (no power flow)"**. Voltages and temperature are unchanged.
- The rule only changes the mode text when the D1 says Boost or Buck.
- The web page Details shows a **"D1 reports"** line with the D1's own mode and currents while the rule is on.
- Setting: `[dbus] idle_current` in `config.ini` (amps, default 2; `0` = off, show exactly what the D1 sends).
- Trade-off: a real transfer of 2 A or less on the 48 V side (about 116 W) is also shown as 0.
- In C, the rule is applied before the 5 s averaging, so the averaged values are 0 too.

## Installing a build

Each build is a `venus-data-nomadic-d1-monitor.tar.gz` file. Keep that exact name, put only one on a USB stick,
insert it in the Cerbo and restart. To build one yourself, check out the branch and run `./make-usb-installer.sh`.
Tests: `python3 -m unittest discover -s tests`.
