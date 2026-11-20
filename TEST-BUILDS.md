# D1 monitor test builds (November 2026)

Three monitor-only builds for bench and field testing. None of them is merged into `main` or published as a
release. The current release is still **2.5.0**. All three only listen to the D1 and never send it anything.

| Label | Version | Branch | What it changes on top of 2.5.0 |
|---|---|---|---|
| **A: Smoothing** | 2.6.0 | [`feature/smoothing-2.6.0`](../../tree/feature/smoothing-2.6.0) (PR #17) | Averages displayed voltage, current and power over 5 s |
| **B: Idle rule** | 2.5.2-idle | [`test/idle-2a`](../../tree/test/idle-2a) | Shows 2 A or less on the 48 V side as Idle, 0 A, 0 W, only with the 12 V side at rest (13.0 V or less) |
| **C: Smoothing + idle** | 2.6.2-test | [`test/smoothing-plus-idle-2.6.1`](../../tree/test/smoothing-plus-idle-2.6.1) | A and B together |

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

- When the D1 reports **2 A or less on the 48 V side** and its **12 V side is at 13.0 V or less** (engine off,
  chassis batteries resting), both currents are shown as 0 A and 0 W, and the mode as **"Idle (no power flow)"**.
  Voltages and temperature are unchanged.
- With the alternators charging (about 14 V), or while bucking with the 12 V bus held at about 13.4 V, nothing
  is hidden: a real small transfer shows as it is (added in 2.5.2-idle / 2.6.2-test).
- The rule only changes the mode text when the D1 says Boost or Buck.
- The web page Details shows a **"D1 reports"** line with the D1's own mode and currents while the rule is on.
- Setting: `[dbus] idle_current` in `config.ini` (amps, default 2; `0` = off, show exactly what the D1 sends).
- Setting: `[dbus] idle_max_12v` (volts, default 13.0; `0` = no voltage condition, as in 2.5.1 / 2.6.1).
- Trade-off: only a real transfer of 2 A or less with the 12 V side at rest is shown as 0.
- In C, the rule is applied before the 5 s averaging, so the averaged values are 0 too.

## When the idle rule hides a reading, and when it doesn't (notes, 2026-11-19)

A reading is shown as Idle, 0 W **only when both** are true: the 48 V side is at 2 A or less (about 116 W)
**and** the D1's 12 V side is at 13.0 V or less. Otherwise everything is shown exactly as the D1 sends it.

| Situation | 12 V side | Shown? |
|---|---|---|
| Parked after a drive, fixed −5/−6 A / +2 A offset | ~12.6 V | Hidden (Idle, 0 W): the phantom 116 W |
| Driving, alternators charging, any boost | ~14 V | Shown |
| Any transfer over 2 A on the 48 V side | any | Shown |
| Buck: Lithionics (e.g. with excess solar) charging the chassis batteries | ~13.4 V | Shown, even a small trickle |
| First seconds after the Egis switches, buck still ramping, 2 A or less | ~12.5 V rising | May show Idle briefly, normal once above 13.0 V |
| Very weak solar on the 12 V side (dawn, heavy overcast), boost of 2 A or less | 13.0 V or less | Hidden |

- **Why weak boost can't be told apart:** the D1 reports current in whole amps and its parked offset already
  reads +2 A, so a real 2 A boost and the phantom look identical in the D1's data. Only Nomadic can say whether
  the D1 has a true zero-current reading or flag.
- **What stays visible:** the web page Details line **"D1 reports"** shows what the D1 actually sent whenever
  the rule is active, and the Victron MPPT reports its own solar watts to the Cerbo separately.
- **Surface charge:** right after parking, the chassis batteries may sit above 13.0 V for a while, so the
  116 W can still show until they settle below 13.0 V.
- **Tuning:** if too much weak solar is hidden, lower `idle_max_12v` (e.g. 12.8). If the 116 W lingers after
  parking, the limit can be raised slightly. `0` turns the voltage condition off.

### Second opinion checked (2026-11-19)

An outside AI answer blamed the 116 W on D1 "float holding" plus the Cerbo's DC Loads math (alternator minus
battery). The DC Loads part is right. The float-holding part doesn't fit the field data: the Lithionics reads
0.0 A (it resolves 0.1 A, so under ~6 W real), the figures don't change with loads, and the D1 reports more
power out (116 W) than in (63–76 W). That points to a current-sensor zero offset. Its suggestions taken:
a deadband (already build B) and the resting-voltage condition (added in 2.5.2-idle / 2.6.2-test). Not taken:
mapping idle to a Fault/Standby state (a parked D1 isn't faulted, and a fault could raise alerts).

**Test plan (2026-11-20):** install B or C, then check (1) parked after a drive: Idle, 0 W once the 12 V side
is at 13.0 V or less; (2) driving: real boost figures; (3) chassis batteries charging from the Lithionics:
mode "Buck 48V to 12V" with real amps and watts.

## Installing a build

Each build is a `venus-data-nomadic-d1-monitor.tar.gz` file. Keep that exact name, put only one on a USB stick,
insert it in the Cerbo and restart. To build one yourself, check out the branch and run `./make-usb-installer.sh`.
Tests: `python3 -m unittest discover -s tests`.
