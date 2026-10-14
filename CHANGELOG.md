# Changelog

What changed in each version of the driver (`VERSION` in `dbus-nomadic-dcdc/dbus-nomadic-dcdc.py`).

This repository starts at 2.0.0, the monitor-only edition. Versions 1.0.0 to 1.3.0 (with control) were developed
in [cerbo-nomadic-d1](https://github.com/CKOCI13/cerbo-nomadic-d1), where they continue; their entries are kept below for reference.

## 2.0.0 (2026-10-14): monitor only

The driver is now a **monitor**: it listens to the D1 and never sends it anything. Control (Automatic / Off / Buck /
Boost, set-points, current limits, precharge, test timer) has been removed completely, because it isn't known what
the D1 does when command frames stop. The control version continues in [cerbo-nomadic-d1](https://github.com/CKOCI13/cerbo-nomadic-d1).

- **Nothing can transmit:** the CAN socket is receive-only, the EMS_DCDC_CTRL encoder is gone, D-Bus has no
  writable values (only screen-item names, cards and visibility), and the web page is read-only (`405` for anything
  but `GET`). A test walks the code to make sure no way to send comes back.
- **New Cerbo screen layout:** three cards: the converter (alarm, status, temperature), its 12 V side and its 48 V
  side (current, **power** and voltage each). The device page has no on/off switch any more.
- **Web page with charts:** power, both voltages and temperature over 10 min / 1 h / 6 h / 24 h, with a shared
  crosshair, an energy-flow strip and a table view. New `GET /api/history`. The history (one 10 s average per point,
  24 hours) is kept in memory.
- **`--monitor`** also prints the power on each side.
- **Simulation:** the pretend D1 now also boosts, driven by a 12 V side that alternates between a load and a
  charging source.
- **`config.ini`:** the `[control]` section is gone. Old files still work; the section is ignored.
- **Documents and pictures rebuilt:** new Installation Guide and Safe Test Plan (four stages, none of which can
  change what the D1 does), new Cerbo, VRM, web-page and explainer pictures, and their sources in `docs/src`
  with a build script.

## 1.3.0 (2026-10-06): safe testing

- **Simulation mode** (`--simulate`, Cerbo Setup option 9). The real driver runs against a pretend D1 and never
  opens a CAN socket. It registers separately, so it can run next to a real installation.
- **Test timer** (`test_timer` in `config.ini`): Buck or Boost switches itself to Off after a set number of minutes.
- **"Find the converter"** now shows each port's bitrate and CAN state, and spots a D1 talking at the other
  speed from the bus error count.
- **Buck target default 13.5 V,** the usual target for charging chassis batteries.
- **Safe Test Plan** document.

## 1.2.0 (2026-10-05): matched to the D1 manual

- **Automatic mode is the default.** The driver sends nothing and the D1 runs on its own logic, exactly as without
  a Cerbo. Off, Buck and Boost take control only when chosen.
- **Separate buck and boost current limits,** because the D1's limits differ by direction (178 A / 57 A).
- **Limits match the D1 manual:** buck target 10–13.5 V, boost target 42–57.8 V.
- **Cable instructions match the D1's 6-pin signal connector:** CAN-H on pin 1, CAN-L on pin 2, KL15 / KL30
  power-up wires, CAN ground to the common ground, and a 120 Ω terminator at the D1.
- **`--scan`** finds which Cerbo port the converter is on.
- **`Cerbo Setup.bat`,** a menu-driven Windows helper that copies, tests and installs the driver.
- Precharge controls are hidden by default. Short status labels fit the GX tiles.

## 1.1.0 (2026-10-05): Victron screen and VRM

- **Readings and controls in the GX switch pane, device page, VRM portal and VRM app,** via Victron's switch API.
- Names, cards and visibility changed on the GX are remembered.
- Alarms are sorted most serious first.

## 1.0.0 (2026-10-05): first version

- Decodes the D1's CAN messages and publishes them on D-Bus as a DC-DC converter.
- Sends the control message every 10 ms, with limits on every value and a pause before reversing direction.
- Built-in web page with a JSON API and an optional password.
- `--monitor` listen-only tool. Installs as a Venus OS service that survives firmware updates.
