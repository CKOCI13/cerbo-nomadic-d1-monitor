#!/usr/bin/env python3
"""Venus OS monitor for the Nomadic D1 bidirectional DC-DC converter (CAN).

Monitor only: it listens to the converter and never sends it anything. It has no code
that can transmit on CAN, so it cannot change what the D1 does.

Runs on a Victron GX device (Cerbo GX, Ekrano GX, ...) and:
  * receives DCDC_PARAMETER, DCDC_ELECTRIC_VALUE and DCDC_VERSION and publishes
    them on D-Bus, so they appear on the GX screen, in the device list, VRM, MQTT
    and Node-RED
  * keeps a 24-hour history of the readings for the charts on its web page
  * keeps running records (lowest/highest voltages and temperature, kWh each way, hours
    boosting and bucking, alarm counts) across restarts, for the GX's History page
  * serves a small read-only web page with live values, alarms and charts

Usage:
  dbus-nomadic-dcdc.py              run as a service (D-Bus + web page)
  dbus-nomadic-dcdc.py --monitor    print decoded values in the terminal
  dbus-nomadic-dcdc.py --scan       find which CAN port the converter is on
  dbus-nomadic-dcdc.py --simulate   run against a pretend D1 (no CAN bus at all)
  dbus-nomadic-dcdc.py --no-dbus    web page without D-Bus (any Linux PC with SocketCAN)
"""

import argparse
import base64
import collections
import configparser
import errno
import hmac
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nomadic_can as nc

VERSION = '2.5.0'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VELIB_PATHS = (
    os.path.join(BASE_DIR, 'ext', 'velib_python'),
    '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python',
)
SYS_NET = '/sys/class/net'
VERSION_FILE = '/opt/victronenergy/version'
HISTORY_INTERVAL = 10.0               # seconds between history points
HISTORY_POINTS = 24 * 3600 // 10      # 24 hours
ENERGY_MAX_GAP = 1.0                  # s; a longer gap between readings is not counted as energy
TOTALS_SAVE_INTERVAL = 300            # s between saves of the running records (and on stop)

log = logging.getLogger('nomadic-dcdc')

DEFAULTS = {
    'can': {
        'interface': 'auto',
        'timeout': '1.0',
    },
    'dbus': {
        'service_class': 'alternator',
        'device_instance': '70',
        'custom_name': 'DC-DC D1',
        'update_interval': '1.0',
        'switch_pane': 'yes',
    },
    'temperature': {
        'warning': '100',
        'alarm': '105',
        'sensor': 'yes',
        'sensor_instance': '71',
    },
    'web': {
        'port': '8088',
        'bind': '0.0.0.0',
        'password': '',
        'temperature_unit': 'auto',
    },
}


def load_config(path):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read_dict(DEFAULTS)
    if os.path.exists(path):
        cfg.read(path)
    else:
        log.warning('%s not found, using built-in defaults', path)
    return cfg


def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning('Could not read %s: %s', path, e)
        return {}


def save_json(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log.warning('Could not save %s: %s', path, e)


# --------------------------------------------------------------------------- telemetry

ELECTRIC_FIELDS = ('lv_voltage', 'lv_current', 'hv_voltage', 'hv_current', 'temperature')
ENERGY_KEYS = ('lv_charged', 'lv_drawn', 'hv_charged', 'hv_drawn')   # Wh

MODE_BOOST = 0x3   # 12 V side -> 48 V side
MODE_BUCK = 0x4    # 48 V side -> 12 V side


def orient(electric, parameter):
    """Give the currents one sign convention: + = flowing into that side's battery.

    The CAN document's example frame is signed that way, but a real D1 boosting at 132 A reported both
    currents as positive. So while the converter says it is boosting or bucking, the direction comes
    from the operating mode: boost takes current out of the 12 V side and puts it into the 48 V side,
    buck the other way round. In every other mode the currents are kept as the converter sends them.
    The values as received stay available as lv_current_reported / hv_current_reported.
    """
    e = dict(electric)
    e['lv_current_reported'], e['hv_current_reported'] = e['lv_current'], e['hv_current']
    mode = parameter and parameter['operating_mode']
    if mode == MODE_BOOST:
        e['lv_current'], e['hv_current'] = -abs(e['lv_current']), abs(e['hv_current'])
    elif mode == MODE_BUCK:
        e['lv_current'], e['hv_current'] = abs(e['lv_current']), -abs(e['hv_current'])
    return e


class Telemetry:
    """Latest decoded frames from the converter, plus running averages for the history."""

    DECODERS = {
        nc.ID_PARAMETER: nc.decode_parameter,
        nc.ID_ELECTRIC: nc.decode_electric,
        nc.ID_VERSION: nc.decode_version,
    }

    def __init__(self, timeout):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._latest = {}
        self._counts = dict.fromkeys(nc.RX_IDS, 0)
        self._sums = dict.fromkeys(ELECTRIC_FIELDS, 0.0)
        self._samples = 0
        self._energy = dict.fromkeys(ENERGY_KEYS, 0.0)
        self._last_electric = None
        self.bad_frames = 0

    def feed(self, can_id, data, now=None):
        decode = self.DECODERS.get(can_id)
        if decode is None:
            return
        if len(data) != 8:
            self.bad_frames += 1
            return
        value = decode(data)
        now = time.monotonic() if now is None else now
        with self._lock:
            if can_id == nc.ID_ELECTRIC:
                parameter = self._latest.get(nc.ID_PARAMETER, (None, None))[1]
                value = orient(value, parameter)
            self._latest[can_id] = (now, value)
            self._counts[can_id] += 1
            if can_id == nc.ID_ELECTRIC:
                for k in ELECTRIC_FIELDS:
                    self._sums[k] += value[k]
                self._samples += 1
                # Energy: each reading's power over the time since the one before (100 a second)
                if self._last_electric is not None and 0 < now - self._last_electric <= ENERGY_MAX_GAP:
                    hours = (now - self._last_electric) / 3600
                    for side in ('lv', 'hv'):
                        w = value[side + '_voltage'] * value[side + '_current']
                        self._energy[side + ('_charged' if w > 0 else '_drawn')] += abs(w) * hours
                self._last_electric = now

    def take_energy(self):
        """Wh into (charged) and out of (drawn) each side's battery since the last call."""
        with self._lock:
            energy, self._energy = self._energy, dict.fromkeys(ENERGY_KEYS, 0.0)
        return energy

    def take_averages(self):
        """Average of every electric reading since the last call, or None if none arrived."""
        with self._lock:
            n, sums = self._samples, self._sums
            self._sums, self._samples = dict.fromkeys(ELECTRIC_FIELDS, 0.0), 0
        if not n:
            return None
        return {k: sums[k] / n for k in ELECTRIC_FIELDS}

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            latest = dict(self._latest)
            counts = dict(self._counts)
        ages, fresh = {}, {}
        for can_id in nc.RX_IDS:
            t, value = latest.get(can_id, (None, None))
            ages[can_id] = None if t is None else now - t
            fresh[can_id] = value if t is not None and now - t <= self.timeout else None
        parameter, electric = fresh[nc.ID_PARAMETER], fresh[nc.ID_ELECTRIC]
        version = latest.get(nc.ID_VERSION, (None, None))[1]   # does not change; keep last
        return {
            'online': parameter is not None or electric is not None,
            'parameter': parameter,
            'electric': electric,
            'version': version,
            'ages': ages,
            'counts': counts,
        }


class History:
    """The last 24 hours of readings, one averaged point every HISTORY_INTERVAL seconds.

    Kept in memory only, so it starts empty after a restart.
    """

    FIELDS = ('time', 'lv_voltage', 'lv_current', 'lv_power', 'hv_voltage', 'hv_current',
              'hv_power', 'temperature')

    def __init__(self, telemetry, interval=HISTORY_INTERVAL, size=HISTORY_POINTS):
        self.telemetry = telemetry
        self.interval = interval
        self._points = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._next = time.monotonic() + interval

    def tick(self, now=None):
        """Call often (every second is fine); adds a point once per interval."""
        now = time.monotonic() if now is None else now
        if now < self._next:
            return
        self._next = max(self._next + self.interval, now)
        self.add(self.telemetry.take_averages())

    def add(self, avg, wall_time=None):
        t = int(time.time() if wall_time is None else wall_time)
        if avg is None:   # offline: a gap in the charts
            point = (t,) + (None,) * (len(self.FIELDS) - 1)
        else:
            point = (t, round(avg['lv_voltage'], 2), round(avg['lv_current'], 1),
                     round(avg['lv_voltage'] * avg['lv_current']),
                     round(avg['hv_voltage'], 2), round(avg['hv_current'], 1),
                     round(avg['hv_voltage'] * avg['hv_current']),
                     round(avg['temperature'], 1))
        with self._lock:
            self._points.append(point)

    def points(self, seconds=None):
        with self._lock:
            points = list(self._points)
        if seconds is not None and points:
            start = points[-1][0] - seconds
            points = [p for p in points if p[0] >= start]
        return points


# The D1's voltage warnings (DCDC_VoltageStatus bits), grouped the way the GX's History page counts them
VOLTAGE_ALARM_BITS = {
    'lv_low': 0b00000011,    # undervoltage / low voltage on the 12 V side
    'lv_high': 0b00001100,   # high voltage / overvoltage on the 12 V side
    'hv_low': 0b00110000,
    'hv_high': 0b11000000,
}
# Modes in which the D1's readings aren't meaningful yet (or any more): not used for lowest/highest
MODES_NOT_RUNNING = (0x0, 0x1, 0x7, 0xF)


class Totals:
    """Running records since the last reset, kept across restarts and reboots.

    The GX's device page shows them under History (lowest/highest voltage on each side, lowest/
    highest temperature, kWh each way, the D1's voltage warning counts) and the web page shows all
    of them. "Clear History" on that page (or the web page's API) starts them again. Only the
    monitor's own file changes; nothing is ever sent to the converter.
    """

    NUMBERS = ('lv_voltage_min', 'lv_voltage_max', 'hv_voltage_min', 'hv_voltage_max',
               'temperature_min', 'temperature_max', 'lv_current_peak', 'hv_current_peak',
               'boost_seconds', 'buck_seconds', 'derating_seconds', 'fault_events',
               'temperature_warnings', 'temperature_alarms', 'last_fault_time') \
        + ENERGY_KEYS + tuple(k + '_alarms' for k in VOLTAGE_ALARM_BITS)
    TEXTS = ('last_fault',)
    MAX_STEP = 5.0   # s; a longer pause between updates isn't counted as running time
    FLOW_W = 20      # W on the 48 V side before the D1 counts as boosting or bucking

    def __init__(self, path=None, save_interval=TOTALS_SAVE_INTERVAL):
        self.path = path
        self.save_interval = save_interval
        self._lock = threading.Lock()
        self._dirty = False
        self._saved_at = None   # set by the first update
        self._last = None
        self._voltage_bits = 0
        self._faulted = False
        self._temperature_level = TEMP_OK
        self._new()
        if path:
            self._load(load_json(path))

    def _new(self):
        self.values = dict.fromkeys(self.NUMBERS + self.TEXTS)
        for k in ENERGY_KEYS + tuple(k + '_alarms' for k in VOLTAGE_ALARM_BITS) + (
                'boost_seconds', 'buck_seconds', 'derating_seconds', 'fault_events',
                'temperature_warnings', 'temperature_alarms'):
            self.values[k] = 0
        self.values['since'] = int(time.time())

    def _load(self, data):
        for k in self.NUMBERS + ('since',):
            v = data.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                self.values[k] = v
        for k in self.TEXTS:
            if isinstance(data.get(k), str):
                self.values[k] = data[k]

    def update(self, snap, energy=None, temperature_level=0, now=None):
        """Call once a second with the latest snapshot and the energy since the last call."""
        now = time.monotonic() if now is None else now
        e, p = snap['electric'], snap['parameter']
        with self._lock:
            v = self.values
            step = 0 if self._last is None else now - self._last
            self._last = now
            if self._saved_at is None:
                self._saved_at = now
            if energy:
                for k in ENERGY_KEYS:
                    if energy.get(k):
                        v[k] += energy[k]
                        self._dirty = True
            mode = p and p['operating_mode']
            if snap['online'] and 0 < step <= self.MAX_STEP:
                # Boost or buck from which way the energy flows (the D1 may also say "Autonomous")
                hv = power(e, 'hv')
                for key, on in (('boost_seconds', hv is not None and hv >= self.FLOW_W),
                                ('buck_seconds', hv is not None and hv <= -self.FLOW_W),
                                ('derating_seconds', mode == 0x8)):
                    if on:
                        v[key] += step
                        self._dirty = True
            if e is not None and p is not None and mode not in MODES_NOT_RUNNING:
                for field in ('lv_voltage', 'hv_voltage', 'temperature'):
                    self._extremes(field, e[field])
                for side in ('lv', 'hv'):
                    peak = abs(e[side + '_current'])
                    if v[side + '_current_peak'] is None or peak > v[side + '_current_peak']:
                        v[side + '_current_peak'] = peak
                        self._dirty = True
            if p is not None:
                bits = p['voltage_status']
                for name, mask in VOLTAGE_ALARM_BITS.items():
                    if bits & mask and not self._voltage_bits & mask:
                        v[name + '_alarms'] += 1
                        self._dirty = True
                self._voltage_bits = bits
                faults = [i for i in nc.active_alarms(p) if i < nc.FAULT_ALARM_COUNT]
                if faults and not self._faulted:
                    v['fault_events'] += 1
                    v['last_fault'] = nc.ALARMS[faults[0]][2]
                    v['last_fault_time'] = int(time.time())
                    self._dirty = True
                self._faulted = bool(faults)
            if temperature_level > self._temperature_level:
                v['temperature_alarms' if temperature_level == TEMP_ALARM else 'temperature_warnings'] += 1
                self._dirty = True
            self._temperature_level = temperature_level
        if self._dirty and now - self._saved_at >= self.save_interval:
            self.save(now)

    def _extremes(self, field, value):
        v = self.values
        if v[field + '_min'] is None or value < v[field + '_min']:
            v[field + '_min'] = value
            self._dirty = True
        if v[field + '_max'] is None or value > v[field + '_max']:
            v[field + '_max'] = value
            self._dirty = True

    def clear(self):
        with self._lock:
            self._new()
            self._dirty = True
        log.info('History cleared')
        self.save()

    def save(self, now=None):
        self._saved_at = time.monotonic() if now is None else now
        if not self.path or not self._dirty:
            return
        with self._lock:
            data, self._dirty = dict(self.values), False
        save_json(self.path, data)

    def as_dict(self):
        """Everything, for the web page: energy in kWh, times in hours."""
        with self._lock:
            v = dict(self.values)
        for k in ENERGY_KEYS:
            v[k.replace('_charged', '_charged_kwh').replace('_drawn', '_drawn_kwh')] = round(v.pop(k) / 1000, 3)
        for k in ('boost', 'buck', 'derating'):
            v[k + '_hours'] = round(v.pop(k + '_seconds') / 3600, 2)
        return v


class StatusWatcher:
    """Logs when the converter goes on/offline and when alarms appear or clear."""

    def __init__(self, interface):
        self._interface = interface
        self.bus = None   # set to the bus, so "auto" is reported as the port it found
        self.online = None
        self.alarms = set()

    @property
    def interface(self):
        return getattr(self.bus, 'interface', None) or self._interface

    def __call__(self, snap):
        if snap['online'] != self.online:
            if snap['online']:
                log.info('Converter online on %s', self.interface)
            else:
                log.warning('Converter OFFLINE: no frames received on %s', self.interface)
            self.online = snap['online']
        alarms = set(nc.alarm_texts(snap['parameter']))
        for a in sorted(alarms - self.alarms):
            log.warning('Alarm: %s', a)
        for a in sorted(self.alarms - alarms):
            log.info('Alarm cleared: %s', a)
        self.alarms = alarms


# --------------------------------------------------------------------------- CAN input

# Linux SocketCAN constants (linux/can.h), written out here instead of taken from Python's socket
# module: on 32-bit Python, as on the Cerbo GX, socket.CAN_EFF_FLAG is a negative number
# (-2147483648), which struct.pack('=I') refuses, so the monitor crashed as soon as it opened a port.
CAN_SFF_MASK = 0x000007FF   # standard (11-bit) ID bits
CAN_EFF_FLAG = 0x80000000   # extended (29-bit) frame
CAN_RTR_FLAG = 0x40000000   # remote request frame


class CanBus:
    """Receive-only SocketCAN access: one socket, filtered to the converter's three messages.

    There is deliberately no send method: the monitor cannot transmit.
    """

    FRAME = struct.Struct('=IB3x8s')   # struct can_frame

    def __init__(self, interface):
        self.interface = interface
        s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        # Standard-ID data frames with exactly these IDs.
        mask = CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
        filters = b''.join(struct.pack('=II', i, mask) for i in nc.RX_IDS)
        try:
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, filters)
        except OSError as e:
            log.warning('Could not set CAN filter: %s', e)
        s.bind((interface,))
        s.settimeout(0.5)
        self._rx = s

    def recv(self):
        """Return (can_id, data), or None if nothing arrived within the timeout."""
        try:
            raw = self._rx.recv(self.FRAME.size)
        except socket.timeout:
            return None
        can_id, dlc, data = self.FRAME.unpack(raw)
        return can_id & CAN_SFF_MASK, data[:dlc]

    def close(self):
        self._rx.close()



AUTO = 'auto'
AUTO_MIN_FRAMES = 5   # frames with the converter's IDs needed to accept a port (it sends ~130 a second)
# A port is only accepted once 8-byte frames of both of these have been heard on it, so a stray frame
# that happens to use one of the converter's IDs on another bus isn't mistaken for the converter.
AUTO_REQUIRED_IDS = (nc.ID_PARAMETER, nc.ID_ELECTRIC)


def can_ports():
    """Names of the CAN interfaces (can0, can1, vecan0, vecan1 ...), from /sys/class/net."""
    try:
        names = sorted(os.listdir(SYS_NET))
    except OSError:
        return []
    return [n for n in names if _read_int(os.path.join(SYS_NET, n, 'type')) == 280]   # ARPHRD_CAN


def find_converter(seconds=1.0):
    """Listen (transmitting nothing) on each CAN port for up to `seconds` and return the first port
    the converter is heard on, or None. Only 8-byte standard-ID frames with the converter's IDs
    count, and both its status (0x066) and its readings (0x088) must be among them."""
    for name in can_ports():
        try:
            bus = CanBus(name)
        except OSError:
            continue
        heard, ids = 0, set()
        try:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                frame = bus.recv()
                if frame and frame[0] in nc.RX_IDS and len(frame[1]) == 8:
                    heard += 1
                    ids.add(frame[0])
                    if heard >= AUTO_MIN_FRAMES and ids.issuperset(AUTO_REQUIRED_IDS):
                        return name
        except OSError:
            pass
        finally:
            bus.close()
    return None


class AutoCanBus:
    """interface = auto: listens on every CAN port until the converter is heard, then uses only that
    port. Monitor only: it never transmits on any port."""

    SEARCH_PAUSE = 2.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.interface = AUTO
        self._bus = None

    def _search(self):
        log.info('interface = auto: looking for the converter on %s',
                 ', '.join(can_ports()) or 'any CAN port (none found yet)')
        last_log = time.monotonic()
        while True:
            name = find_converter()
            if name:
                bus = CanBus(name)
                self.cfg['can']['interface'] = name
                self.interface = name
                self._bus = bus
                log.info('Converter found on %s; using that port', name)
                return
            if time.monotonic() - last_log >= 60:
                last_log = time.monotonic()
                log.warning('Converter still not heard on %s. Check its power, the CAN cable and '
                            'terminators, and the port\'s CAN-bus profile (250 / 500 kbit/s).',
                            ', '.join(can_ports()) or 'any CAN port')
            time.sleep(self.SEARCH_PAUSE)

    def recv(self):
        if self._bus is None:
            self._search()
        return self._bus.recv()

    def close(self):
        if self._bus is not None:
            self._bus.close()


class SimulatedD1:
    """A pretend D1 for --simulate. Nothing touches a CAN bus.

    It follows the D1 manual's automatic logic: buck below 12.5 V while the 48 V side is
    41-58 V (up to about 13.4 V), boost when the 12 V side is 13.5-16 V until it falls below
    12.3 V, standby otherwise. To show every mode, the pretend 12 V side alternates every
    three minutes between a load (draining it) and a charging source such as an alternator.
    It is for trying out the screens, not for predicting what the real converter will do.
    """

    interface = 'simulated'
    LOAD = 15.0             # A drawn by the pretend 12 V loads
    CHARGE = 30.0           # A pushed in by the pretend alternator
    PHASE = 180.0           # s of each, alternately
    BOOST_DRAW = 40.0       # A the pretend D1 takes from the 12 V side when boosting
    K_LV = 0.0005           # V per A*s on the pretend 12 V battery
    K_HV = 0.0002           # V per A*s on the pretend 48 V battery
    GAIN = 200.0            # A per V of error when regulating the 12 V side
    PERIODS = {nc.ID_ELECTRIC: 0.010, nc.ID_PARAMETER: 0.050, nc.ID_VERSION: 0.100}

    def __init__(self):
        self._lock = threading.Lock()
        self.lv, self.hv, self.temp = 12.6, 51.2, 30.0
        self.lv_i = self.hv_i = 0.0
        self.mode, self.state = 0x2, 'standby'
        now = time.monotonic()
        self._start = now
        self._due = dict.fromkeys(self.PERIODS, now)
        self._last_step = now

    def recv(self):
        """The next frame when it's due, like a socket with a 0.5 s timeout."""
        can_id = min(self._due, key=self._due.get)
        delay = self._due[can_id] - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.5))
            if delay > 0.5:
                return None
        now = time.monotonic()
        self._due[can_id] = max(self._due[can_id] + self.PERIODS[can_id], now - 1.0)
        with self._lock:
            self.step(now)
            return can_id, self._frame(can_id)

    def close(self):
        pass

    def external(self, now):
        """Net current into the 12 V battery from everything but the D1."""
        charging = int((now - self._start) // self.PHASE) % 2 == 1
        return self.CHARGE if charging else -self.LOAD

    def step(self, now):
        dt = min(max(now - self._last_step, 0.0), 1.0)
        self._last_step = now
        buck_ok = 41.0 <= self.hv <= 58.0
        if self.state == 'buck' and (self.lv > 13.5 or not buck_ok):
            self.state = 'standby'
        elif self.state == 'boost' and self.lv < 12.3:
            self.state = 'standby'
        elif self.state == 'standby':
            if self.lv < 12.5 and buck_ok:
                self.state = 'buck'
            elif 13.5 < self.lv <= 16.0 and self.hv < 57.5:
                self.state = 'boost'
        external = self.external(now)
        lv_i = hv_i = 0.0
        if self.state == 'buck':
            lv_i = max(0.0, min(178.0, -external + (13.4 - self.lv) * self.GAIN))
            hv_i = -(self.lv * lv_i) / (self.hv * 0.94)
        elif self.state == 'boost':
            lv_i = -self.BOOST_DRAW
            hv_i = (self.lv * self.BOOST_DRAW * 0.92) / self.hv
        self.mode = 0x2 if self.state == 'standby' else 0x5
        # positive current = into that side's battery
        self.lv_i, self.hv_i = lv_i, hv_i
        self.lv = max(11.0, min(15.0, self.lv + (lv_i + external) * self.K_LV * dt))
        self.hv = max(44.0, min(57.5, self.hv + hv_i * self.K_HV * dt))
        target_temp = 30.0 + abs(self.lv * lv_i) / 100.0
        self.temp += (target_temp - self.temp) * dt / 60.0

    def _frame(self, can_id):
        if can_id == nc.ID_ELECTRIC:
            return nc.encode_electric(self.lv, self.lv_i, self.hv, self.hv_i, self.temp)
        if can_id == nc.ID_PARAMETER:
            voltage_status = (0x02 if self.lv < 12.3 else 0) | (0x04 if self.lv > 14.8 else 0)
            return nc.encode_parameter(voltage_status=voltage_status, operating_mode=self.mode)
        return nc.encode_version(0x100, 1)


def simulation_config(cfg):
    """Run beside a real installation without clashing: own D-Bus name, instance, web port, files."""
    cfg['can']['interface'] = 'sim'
    d = cfg['dbus']
    d['device_instance'] = str(d.getint('device_instance') + 1)
    d['custom_name'] = d['custom_name'] + ' (SIMULATED)'
    t = cfg['temperature']
    t['sensor_instance'] = str(t.getint('sensor_instance') + 1)
    if cfg['web'].getint('port'):
        cfg['web']['port'] = str(cfg['web'].getint('port') + 1)


def rx_worker(bus, telemetry):
    while True:
        try:
            frame = bus.recv()
        except OSError as e:
            if e.errno != errno.ENETDOWN:
                raise
            log.warning('%s is down', bus.interface)
            time.sleep(1)
            continue
        if frame:
            telemetry.feed(*frame)


def start_thread(name, target, *args):
    def run():
        try:
            target(*args)
        except Exception:
            log.exception('%s thread failed; exiting so the service restarts', name)
            os._exit(1)
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- D-Bus

def victron_state(parameter):
    """Map the converter's operating mode onto the Victron charger state enum."""
    if parameter is None:
        return None
    if parameter['fault_status'] & 0b110:   # temporary or permanent failure
        return 2                            # Fault
    return {
        0x1: 245,   # Starting up
        0x2: 0,     # Off
        0x3: 11,    # Power supply (regulating)
        0x4: 11,
        0x5: 11,
        0x7: 0,
        0x8: 11,
    }.get(parameter['operating_mode'])


def _text(unit, digits):
    def fmt(path, value):
        return '---' if value is None else '%.*f%s' % (digits, value, unit)
    return fmt


# Read-only values (GenericInput) for the GX switch pane, the GX device page and the VRM
# "Switches" panel / app widget. API: https://github.com/victronenergy/venus/wiki/dbus#switch
INPUT_DISCRETE = 0      # GenericInput: integer shown as text through /Settings/Labels
INPUT_VALUE = 1         # GenericInput: number with a unit
SHOW_LOCAL_REMOTE = 6   # /Settings/ShowUIInput default from the API

NO_COMMUNICATION = 16   # extra entry after the 16 operating modes
# Short operating-mode texts that fit a GX tile (D1 manual wording: "Standby" for idle)
SHORT_MODES = {0x1: 'Starting up', 0x2: 'Standby', 0x3: 'Boost 12V to 48V', 0x4: 'Buck 48V to 12V',
               0x5: 'Autonomous', 0x7: 'Shutting down', 0x8: 'Derating (hot)', 0xF: 'Invalid'}
MODE_LABELS = [SHORT_MODES.get(m, 'Reserved') for m in range(16)] + ['No communication']
# 0 is "Reserved" in the CAN document; the D1/D2 send it because they don't use precharge.
PRECHARGE_LABELS = ['Not used', 'In progress', 'Not active', 'Invalid']
# After the D1's own alarms: the monitor's temperature levels ([temperature] in config.ini)
TEMP_ALARM_LABELS = ['Temperature high (warning)', 'Temperature very high (alarm)']
ALARM_LABELS = ['None'] + [text for _, _, text in nc.ALARMS] + TEMP_ALARM_LABELS

# The Cerbo screen: three cards. The device card ("DC-DC D1") holds the state of the
# converter; one card per battery side holds its voltage, current and power. Items on a card
# sort by name.
#   (channel, name on its card, type, unit, decimals, labels, card)
# card None = the device's own card; 'lv' / 'hv' = "<custom_name> 12 V side" / "48 V side".
CARDS = {'lv': '%s 12 V side', 'hv': '%s 48 V side'}
# VRM's Inputs page ignores the cards and lists "<custom name>: <name>", so each side's items
# get a /Name that says which side ("Current: 12 V current"); the cards show the custom name.
SIDES = {'lv': '12 V', 'hv': '48 V'}
INPUTS = (
    ('alarm', 'Alarm', INPUT_DISCRETE, None, None, ALARM_LABELS, None),
    ('status', 'Status', INPUT_DISCRETE, None, None, MODE_LABELS, None),
    ('temperature', 'Temperature', INPUT_VALUE, '/Temperature', 0, None, None),
    ('precharge_status', 'Precharge status', INPUT_DISCRETE, None, None, PRECHARGE_LABELS, None),
    ('lv_voltage', 'Voltage', INPUT_VALUE, 'V', 1, None, 'lv'),
    ('lv_current', 'Current', INPUT_VALUE, 'A', 0, None, 'lv'),
    ('lv_power', 'Power', INPUT_VALUE, 'W', 0, None, 'lv'),
    ('hv_voltage', 'Voltage', INPUT_VALUE, 'V', 1, None, 'hv'),
    ('hv_current', 'Current', INPUT_VALUE, 'A', 0, None, 'hv'),
    ('hv_power', 'Power', INPUT_VALUE, 'W', 0, None, 'hv'),
)
# The D1 manual doesn't use precharge, so it starts hidden (show it from the device page).
HIDDEN_BY_DEFAULT = ('precharge_status',)


def power(electric, side):
    """Power on one side in W (V x A, sign as the converter reports the current), or None."""
    if electric is None:
        return None
    return round(electric[side + '_voltage'] * electric[side + '_current'])


# --------------------------------------------------------------------------- temperature levels

# The D1's power-board temperature (D1 manual Rev D, section IV.B, "Thermal Protection & Derating"):
#   -40 to 105 degC  full rated output
#   105 to 120 degC  current derated in a straight line from 100 % down to 0 %
#   above 120 degC   conversion stopped, the D1 reports a fault
# The D1 says nothing until it derates, so the monitor raises its own warning and alarm earlier.
D1_DERATE_C = 105
D1_SHUTDOWN_C = 120
TEMP_OK, TEMP_WARNING, TEMP_ALARM = 0, 1, 2   # same codes as Victron's /Alarms/* paths
TEMP_HYSTERESIS = 3.0   # degC below a level before it clears, so it doesn't flicker on and off


def c_to_f(c):
    return c * 9 / 5 + 32


class TemperatureWatch:
    """Warning / alarm level from the D1's board temperature, with a little hysteresis.

    [temperature] warning and alarm in config.ini are in degC (defaults 100 and 105 = 212 and
    221 degF). No reading (converter offline) clears the level.
    """

    def __init__(self, cfg):
        t = cfg['temperature']
        self.warning = t.getfloat('warning')
        self.alarm = t.getfloat('alarm')
        if self.alarm < self.warning:
            log.warning('[temperature] alarm (%g) is below warning (%g); using %g for both',
                        self.alarm, self.warning, self.alarm)
            self.warning = self.alarm
        self.level = TEMP_OK
        self.temperature = None

    def update(self, snap):
        e = snap['electric']
        c = None if e is None else e['temperature']
        self.temperature = c
        if c is None:
            self.level = TEMP_OK
            return self.level
        level = TEMP_ALARM if c >= self.alarm else TEMP_WARNING if c >= self.warning else TEMP_OK
        if level < self.level:   # falling: hold each level until clearly below its threshold
            if self.level == TEMP_ALARM and c > self.alarm - TEMP_HYSTERESIS:
                level = TEMP_ALARM
            elif level == TEMP_OK and c > self.warning - TEMP_HYSTERESIS:
                level = TEMP_WARNING
        self.level = level
        return level

    def text(self, unit='F'):
        """Plain-words message for the web page, or None while the temperature is fine."""
        if self.level == TEMP_OK or self.temperature is None:
            return None

        def show(c):
            return '%.0f °F' % c_to_f(c) if unit == 'F' else '%.0f °C' % c
        name, limit = ('alarm', self.alarm) if self.level == TEMP_ALARM else ('warning', self.warning)
        return ('Converter temperature %s, above the %s level (%s). The D1 cuts its current back from %s '
                'and stops above %s.' % (show(self.temperature), name, show(limit), show(D1_DERATE_C),
                                         show(D1_SHUTDOWN_C)))


class UiSettings:
    """Switch-pane names, cards and visibility as changed on the GX, kept across restarts."""

    def __init__(self, path):
        self.path = path
        self.values = load_json(path)

    def get(self, key, default):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        save_json(self.path, self.values)


class DbusService:
    """Publishes the converter as com.victronenergy.<service_class>.nomadic_<interface>.

    Dc/0 is the LV (12 V) side, Dc/In the HV (48 V) side, in both directions. Every value is
    read-only; the only writable paths are the switch-pane display settings (names, cards,
    visibility) and /History/Clear, which starts the monitor's own records again.
    """

    def __init__(self, cfg, ui_settings, temperature_watch=None, totals=None):
        for p in VELIB_PATHS:
            if os.path.isdir(p):
                sys.path.insert(1, p)
                break
        from vedbus import VeDbusService

        self.ui = ui_settings
        self.temp = temperature_watch or TemperatureWatch(cfg)
        self.totals = totals or Totals()
        d = cfg['dbus']
        iface = cfg['can']['interface']
        name = 'com.victronenergy.%s.nomadic_%s' % (
            d['service_class'], re.sub(r'[^A-Za-z0-9_]', '_', iface))
        try:
            svc = VeDbusService(name, register=False)
            needs_register = True
        except TypeError:   # velib from before 2024 registers immediately
            svc = VeDbusService(name)
            needs_register = False
        self.svc = svc

        svc.add_path('/Mgmt/ProcessName', os.path.abspath(__file__))
        svc.add_path('/Mgmt/ProcessVersion', VERSION)
        self.can_cfg = cfg['can']
        svc.add_path('/Mgmt/Connection', 'CAN ' + iface + ' (monitor only)')
        svc.add_path('/DeviceInstance', d.getint('device_instance'))
        svc.add_path('/ProductId', 0xFFFF)
        svc.add_path('/ProductName', 'Nomadic D1 DC-DC converter')
        svc.add_path('/CustomName', d['custom_name'])
        svc.add_path('/FirmwareVersion', None)
        svc.add_path('/HardwareVersion', None)
        svc.add_path('/Connected', 1)

        # Which side is the "output" (Dc/0) Victron's screens and totals see:
        #   alternator: the 48 V side, the battery it charges (the Overview gets a tile, and the
        #               GX's DC loads/system figure counts what the D1 puts into or takes out of it)
        #   dcdc and any other class: the 12 V side, as for a DC-DC charger
        # Dc/0 is + when current flows into that battery; Dc/In is + when current flows from the
        # other side into the converter. Both are negative while the converter runs the other way.
        self.output_side = 'hv' if d['service_class'] == 'alternator' else 'lv'
        V, A, W = _text('V', 2), _text('A', 0), _text('W', 0)
        for path, fmt in (('/Dc/0/Voltage', V), ('/Dc/0/Current', A), ('/Dc/0/Power', W),
                          ('/Dc/In/V', V), ('/Dc/In/I', A), ('/Dc/In/P', W)):
            svc.add_path(path, None, gettextcallback=fmt)
        # The D1's board temperature. As an alternator the GX shows it on the Overview tile (top
        # right, in the GX's unit) and on the device page as "Alternator temperature". As a dcdc
        # the device page would call it "Battery temperature", so it's left out there; the
        # temperature sensor below, the switch pane and the web page show it in either case.
        self.publish_temperature = d['service_class'] == 'alternator'
        if self.publish_temperature:
            svc.add_path('/Dc/0/Temperature', None, gettextcallback=_text('C', 0))
        # 0 ok, 1 warning, 2 alarm: the GX raises a "High Temperature" notification from it (for
        # an alternator), shows it on the Notifications page and passes it to VRM.
        svc.add_path('/Alarms/HighTemperature', 0)
        svc.add_path('/State', None)

        # History page (Device list > the D1 > History), Victron's DC meter layout. "Voltage" is the
        # output side (Dc/0), "Aux voltage" the other side, as /Settings/HasStarterVoltage asks for.
        # The alarm counts are the D1's own voltage warnings. "Produced energy" is what went into the
        # output side's battery, "Consumed energy" what was taken out of it.
        kwh = _text('kWh', 2)
        for path, fmt in (('/History/MinimumVoltage', V), ('/History/MaximumVoltage', V),
                          ('/History/MinimumStarterVoltage', V), ('/History/MaximumStarterVoltage', V),
                          ('/History/LowVoltageAlarms', None), ('/History/HighVoltageAlarms', None),
                          ('/History/LowStarterVoltageAlarms', None),
                          ('/History/HighStarterVoltageAlarms', None),
                          ('/History/MinimumTemperature', _text('C', 0)),
                          ('/History/MaximumTemperature', _text('C', 0)),
                          ('/History/EnergyOut', kwh), ('/History/EnergyIn', kwh)):
            svc.add_path(path, None, gettextcallback=fmt)
        svc.add_path('/Settings/HasStarterVoltage', 1)
        svc.add_path('/Settings/HasTemperature', 1)
        svc.add_path('/History/CanBeCleared', 1)
        svc.add_path('/History/Clear', None, writeable=True, onchangecallback=self._on_clear)

        for path in ('/Nomadic/Online', '/Nomadic/OperatingMode', '/Nomadic/OperatingModeText',
                     '/Nomadic/PrechargeStatus', '/Nomadic/PrechargeStatusText',
                     '/Nomadic/LoadStatus', '/Nomadic/FaultStatus', '/Nomadic/VoltageStatus',
                     '/Nomadic/Faults', '/Nomadic/Alarms', '/Nomadic/SoftwareVersion'):
            svc.add_path(path, None)

        self.switch_pane = d.getboolean('switch_pane')
        if self.switch_pane:
            for spec in INPUTS:
                self._add_input(d['custom_name'], *spec)

        if needs_register:
            svc.register()
        log.info('Registered %s on D-Bus (monitor only)', name)

        t = cfg['temperature']
        self.sensor = TemperatureSensor(cfg, ui_settings, VeDbusService) if t.getboolean('sensor') else None

    def _add_input(self, device_name, channel, name, input_type, unit, decimals, labels, card):
        base = '/GenericInput/' + channel
        add = self.svc.add_path
        add(base + '/Name', '%s %s' % (SIDES[card], name.lower()) if card else name)
        add(base + '/Value', None)
        add(base + '/Status', 0)
        add(base + '/Settings/Type', input_type)
        add(base + '/Settings/ValidTypes', 1 << input_type)
        if unit:
            add(base + '/Settings/Unit', unit)
        if decimals is not None:
            add(base + '/Settings/Decimals', decimals)
        if labels:
            add(base + '/Settings/Labels', labels)
        if channel == 'alarm':
            add(base + '/Settings/PrimaryLabel', None)   # "3 active" when there are several
        # On a named card the GUI prefixes the device name unless a custom name is set.
        self._add_ui_setting(base + '/Settings/CustomName', name if card else None)
        self._add_ui_setting(base + '/Settings/Group', CARDS[card] % device_name if card else None)
        self._add_ui_setting(base + '/Settings/ShowUIInput',
                             0 if channel in HIDDEN_BY_DEFAULT else SHOW_LOCAL_REMOTE)

    def _add_ui_setting(self, path, default):
        self.svc.add_path(path, self.ui.get(path, default), writeable=True,
                          onchangecallback=self._on_ui_setting)

    def _on_clear(self, path, value):
        """The GX's "Clear History" button. Starts the monitor's records again; nothing reaches the D1."""
        if value is not None:
            self.totals.clear()
            self._refresh_history()
        return True

    def _refresh_history(self):
        s, v = self.svc, self.totals.values
        out = self.output_side
        inp = 'lv' if out == 'hv' else 'hv'
        s['/History/MinimumVoltage'] = v[out + '_voltage_min']
        s['/History/MaximumVoltage'] = v[out + '_voltage_max']
        s['/History/MinimumStarterVoltage'] = v[inp + '_voltage_min']
        s['/History/MaximumStarterVoltage'] = v[inp + '_voltage_max']
        s['/History/LowVoltageAlarms'] = v[out + '_low_alarms']
        s['/History/HighVoltageAlarms'] = v[out + '_high_alarms']
        s['/History/LowStarterVoltageAlarms'] = v[inp + '_low_alarms']
        s['/History/HighStarterVoltageAlarms'] = v[inp + '_high_alarms']
        s['/History/MinimumTemperature'] = v['temperature_min']
        s['/History/MaximumTemperature'] = v['temperature_max']
        s['/History/EnergyOut'] = round(v[out + '_charged'] / 1000, 3)
        s['/History/EnergyIn'] = round(v[out + '_drawn'] / 1000, 3)

    def _on_ui_setting(self, path, value):
        if path.endswith(('/CustomName', '/Group')):
            ok = value is None or (isinstance(value, str) and len(value.encode('utf-8')) <= 32)
        else:   # /ShowUIInput bitfield
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 15
        if ok:
            self.ui.set(path, value)
        else:
            log.warning('Rejected D-Bus write %s = %r', path, value)
        return ok

    def refresh(self, snap):
        connection = 'CAN ' + self.can_cfg['interface'] + ' (monitor only)'
        if self.svc['/Mgmt/Connection'] != connection:   # interface = auto has found its port
            self.svc['/Mgmt/Connection'] = connection
        s = self.svc
        e, p, v = snap['electric'], snap['parameter'], snap['version']

        out = self.output_side
        inp = 'lv' if out == 'hv' else 'hv'
        s['/Dc/0/Voltage'] = e and e[out + '_voltage']
        s['/Dc/0/Current'] = e and e[out + '_current']
        s['/Dc/0/Power'] = power(e, out)
        s['/Dc/In/V'] = e and e[inp + '_voltage']
        s['/Dc/In/I'] = e and -e[inp + '_current']
        s['/Dc/In/P'] = e and -power(e, inp)
        s['/State'] = victron_state(p)
        level = self.temp.update(snap)
        if self.publish_temperature:
            s['/Dc/0/Temperature'] = e and e['temperature']
        s['/Alarms/HighTemperature'] = level
        if self.sensor is not None:
            self.sensor.refresh(e and e['temperature'], snap['online'])
        self._refresh_history()
        if s['/History/Clear'] is not None:   # ready for the next press (velib ignores a repeat)
            s['/History/Clear'] = None
        s['/FirmwareVersion'] = v and v['software_raw']
        s['/HardwareVersion'] = v and v['hardware']

        s['/Nomadic/Online'] = int(snap['online'])
        s['/Nomadic/OperatingMode'] = p and p['operating_mode']
        s['/Nomadic/OperatingModeText'] = p and p['operating_mode_text']
        s['/Nomadic/PrechargeStatus'] = p and p['precharge_status']
        s['/Nomadic/PrechargeStatusText'] = p and p['precharge_status_text']
        s['/Nomadic/LoadStatus'] = p and p['load_status']
        s['/Nomadic/FaultStatus'] = p and p['fault_status']
        s['/Nomadic/VoltageStatus'] = p and p['voltage_status']
        s['/Nomadic/Faults'] = p and p['faults']
        s['/Nomadic/Alarms'] = None if p is None else '; '.join(nc.alarm_texts(p))
        s['/Nomadic/SoftwareVersion'] = v and v['software']

        if self.switch_pane:
            self._refresh_switch_pane(snap)

    def _refresh_switch_pane(self, snap):
        s = self.svc
        e, p = snap['electric'], snap['parameter']
        alarms = nc.active_alarms(p)
        has_fault = any(i < nc.FAULT_ALARM_COUNT for i in alarms) or self.temp.level == TEMP_ALARM
        if self.temp.level != TEMP_OK:   # after the D1's own alarms
            alarms = alarms + [len(nc.ALARMS) + self.temp.level - 1]

        s['/GenericInput/alarm/Value'] = None if p is None else (alarms[0] + 1 if alarms else 0)
        s['/GenericInput/alarm/Status'] = 1 if has_fault else 0
        s['/GenericInput/alarm/Settings/PrimaryLabel'] = (
            '%d active' % len(alarms) if len(alarms) > 1 else None)
        if not snap['online']:
            s['/GenericInput/status/Value'] = NO_COMMUNICATION
        else:
            s['/GenericInput/status/Value'] = p and p['operating_mode']
        s['/GenericInput/status/Status'] = 0 if snap['online'] else 1
        for channel in ELECTRIC_FIELDS:
            s['/GenericInput/%s/Value' % channel] = e and e[channel]
        s['/GenericInput/lv_power/Value'] = power(e, 'lv')
        s['/GenericInput/hv_power/Value'] = power(e, 'hv')
        s['/GenericInput/precharge_status/Value'] = p and p['precharge_status']


class TemperatureSensor:
    """The D1's board temperature as its own GX temperature sensor (com.victronenergy.temperature).

    That puts it where the GX shows temperature sensors: a gauge on Levels > Environment, an entry
    in the device list, a choice for the Brief page's centre circle, the relay's temperature
    conditions and VRM. Its type is "Water heater", the only sensor type whose gauge (0 to 100 degC,
    32 to 212 degF) covers the D1's normal working range: a working D1 reads around 80 degC (180 degF),
    beyond the Generic gauge's 60 degC (140 degF). The type can be changed on the sensor's Setup page.
    Read-only: nothing here reaches the converter.
    """

    TYPE_WATER_HEATER = 5
    TYPES = range(7)   # battery, fridge, generic, room, outdoor, water heater, freezer

    def __init__(self, cfg, ui_settings, service_class):
        self.ui = ui_settings
        d, iface = cfg['dbus'], cfg['can']['interface']
        name = 'com.victronenergy.temperature.nomadic_%s' % re.sub(r'[^A-Za-z0-9_]', '_', iface)
        bus = private_bus()   # a second service in one process needs its own bus connection
        try:
            svc = service_class(name, bus=bus, register=False)
            needs_register = True
        except TypeError:
            svc = service_class(name, bus)
            needs_register = False
        self.svc = svc
        svc.add_path('/Mgmt/ProcessName', os.path.abspath(__file__))
        svc.add_path('/Mgmt/ProcessVersion', VERSION)
        svc.add_path('/Mgmt/Connection', 'CAN ' + iface + ' (monitor only)')
        svc.add_path('/DeviceInstance', cfg['temperature'].getint('sensor_instance'))
        svc.add_path('/ProductId', 0xFFFF)
        svc.add_path('/ProductName', 'Nomadic D1 board temperature')
        svc.add_path('/CustomName', d['custom_name'])
        svc.add_path('/Connected', 1)
        svc.add_path('/Temperature', None, gettextcallback=_text('C', 1))
        svc.add_path('/Status', 0)   # 0 = OK
        svc.add_path('/TemperatureType', self.ui.get('/Sensor/TemperatureType', self.TYPE_WATER_HEATER),
                     writeable=True, onchangecallback=self._on_type)
        if needs_register:
            svc.register()
        log.info('Registered %s on D-Bus (monitor only)', name)

    def _on_type(self, path, value):
        ok = isinstance(value, int) and not isinstance(value, bool) and value in self.TYPES
        if ok:
            self.ui.set('/Sensor/TemperatureType', value)
        else:
            log.warning('Rejected D-Bus write %s = %r', path, value)
        return ok

    def refresh(self, temperature, online):
        self.svc['/Temperature'] = temperature   # None (shown as ---) while the D1 is silent


def private_bus():
    """Its own connection to the system D-Bus, or None where there is no D-Bus (the tests)."""
    try:
        import dbus
    except ImportError:
        return None
    if 'DBUS_SESSION_BUS_ADDRESS' in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


# --------------------------------------------------------------------------- temperature unit

GX_TEMPERATURE_UNIT = ('com.victronenergy.settings', '/Settings/System/Units/Temperature')
TEMPERATURE_UNITS = {'c': 'C', 'celsius': 'C', 'f': 'F', 'fahrenheit': 'F'}


def read_gx_setting(service, path):
    """One value from another D-Bus service (used for the GX display settings)."""
    import dbus
    bus = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
    return bus.get_object(service, path).GetValue(dbus_interface='com.victronenergy.BusItem', timeout=2)


class TemperatureUnit:
    """The unit the web page shows temperatures in: 'F' or 'C'.

    [web] temperature_unit = auto follows the GX's own setting (Settings > General > Display &
    Appearance > Data units > Temperature), which the GX screen and VRM already use for the
    driver's readings. Fahrenheit or celsius in config.ini forces one. When auto can't read the
    GX setting (on a PC, or with --no-dbus) the page shows Fahrenheit.
    """
    FALLBACK = 'F'
    READ_EVERY = 10.0   # seconds; a change on the GX shows on the page within this time

    def __init__(self, configured, reader=None):
        text = (configured or 'auto').strip().lower()
        self.forced = TEMPERATURE_UNITS.get(text)
        if self.forced is None and text != 'auto':
            log.warning('[web] temperature_unit = %s not understood (use auto, fahrenheit or celsius); '
                        'using auto', configured)
        self.reader = None if self.forced else reader
        self.unit = self.forced or self.FALLBACK
        self._next_read = 0.0

    def poll(self, now=None):
        if self.reader is None:
            return
        now = time.monotonic() if now is None else now
        if now < self._next_read:
            return
        self._next_read = now + self.READ_EVERY
        try:
            value = str(self.reader(*GX_TEMPERATURE_UNIT)).strip().lower()
        except Exception as e:   # settings service restarting, or no D-Bus: keep the last unit
            log.debug('GX temperature unit not readable: %s', e)
            return
        if value in TEMPERATURE_UNITS:
            self.unit = TEMPERATURE_UNITS[value]


# --------------------------------------------------------------------------- web page

class WebApp:
    def __init__(self, cfg, telemetry, history, temperature_unit=None, temperature_watch=None,
                 totals=None):
        self.telemetry = telemetry
        self.totals = totals
        self.temperature_watch = temperature_watch or TemperatureWatch(cfg)
        self.temperature_unit = temperature_unit or TemperatureUnit(cfg['web']['temperature_unit'])
        self.history = history
        self.name = cfg['dbus']['custom_name']
        self.can_cfg = cfg['can']   # live: shows the port "auto" found
        self.password = cfg['web']['password']
        with open(os.path.join(BASE_DIR, 'webui.html'), 'rb') as f:
            self.page = f.read()

    def status(self):
        snap = self.telemetry.snapshot()
        e = snap['electric']
        watch = self.temperature_watch
        watch.update(snap)
        temperature_text = watch.text(self.temperature_unit.unit)
        alarms = nc.alarm_texts(snap['parameter']) + ([temperature_text] if temperature_text else [])
        return {
            'name': self.name,
            'interface': self.can_cfg['interface'],
            'driver_version': VERSION,
            'monitor_only': True,
            'temperature_unit': self.temperature_unit.unit,
            'online': snap['online'],
            'electric': e,
            'power': {'lv': power(e, 'lv'), 'hv': power(e, 'hv')},
            'parameter': snap['parameter'],
            'alarms': alarms,
            'temperature_level': watch.level,
            'temperature_limits': {'warning': watch.warning, 'alarm': watch.alarm,
                                   'derate': D1_DERATE_C, 'shutdown': D1_SHUTDOWN_C},
            'version': snap['version'],
            'totals': self.totals and self.totals.as_dict(),
            'rx_age': {'0x%03X' % k: None if a is None else round(a, 2)
                       for k, a in snap['ages'].items()},
            'rx_count': {'0x%03X' % k: n for k, n in snap['counts'].items()},
        }

    def history_json(self, query):
        m = re.search(r'(?:^|&)minutes=(\d+)', query)
        minutes = min(int(m.group(1)), 24 * 60) if m else 60
        return {
            'interval': self.history.interval,
            'temperature_unit': self.temperature_unit.unit,
            'fields': History.FIELDS,
            'points': self.history.points(minutes * 60),
        }


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'nomadic-dcdc/' + VERSION

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, content_type, extra=()):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj).encode(), 'application/json', extra)

        def _authorized(self):
            if not app.password:
                return True
            header = self.headers.get('Authorization', '')
            if header.startswith('Basic '):
                try:
                    password = base64.b64decode(header[6:]).decode().partition(':')[2]
                    if hmac.compare_digest(password.encode(), app.password.encode()):
                        return True
                except ValueError:
                    pass
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="DC-DC converter"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return
            path, _, query = self.path.partition('?')
            if path == '/':
                self._send(200, app.page, 'text/html; charset=utf-8')
            elif path == '/api/status':
                self._json(200, app.status())
            elif path == '/api/history':
                self._json(200, app.history_json(query))
            else:
                self._json(404, {'error': 'not found'})

        def _read_only(self):
            self._json(405, {'error': 'this is a monitor: it is read-only'}, (('Allow', 'GET'),))

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only

    return Handler


def start_web(cfg, app):
    bind, port = cfg['web']['bind'], cfg['web'].getint('port')
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(app))
    except OSError as e:
        log.error('Web page disabled: cannot listen on %s:%d (%s)', bind, port, e)
        return
    server.daemon_threads = True
    start_thread('web', server.serve_forever)
    log.info('Web page on http://%s:%d/', bind, port)


# --------------------------------------------------------------------------- terminal monitor

def run_monitor(interface, timeout):
    """Prints decoded frames once a second (Cerbo Setup option 3)."""
    if interface == AUTO:
        print('interface = auto: looking for the converter on every CAN port (nothing is transmitted)...')
        interface = None
        while interface is None:
            interface = find_converter()
        print('Converter found on %s.' % interface)
    bus = CanBus(interface)
    telemetry = Telemetry(timeout)
    start_thread('can-rx', rx_worker, bus, telemetry)
    print('Listening on %s (monitor only: nothing is ever transmitted). Ctrl+C to stop.' % interface)
    previous = dict.fromkeys(nc.RX_IDS, 0)
    while True:
        time.sleep(1)
        snap = telemetry.snapshot()
        rates = ' '.join('0x%03X=%d/s' % (i, snap['counts'][i] - previous[i]) for i in nc.RX_IDS)
        previous = snap['counts']
        print('--- %s %s  %s  %s' % (interface, time.strftime('%H:%M:%S'),
                                     'ONLINE' if snap['online'] else 'NO DATA', rates))
        e, p, v = snap['electric'], snap['parameter'], snap['version']
        if e:
            print('LV %5.1f V %+4d A %+6d W | HV %5.1f V %+4d A %+6d W | %d C' % (
                e['lv_voltage'], e['lv_current'], power(e, 'lv'), e['hv_voltage'],
                e['hv_current'], power(e, 'hv'), e['temperature']))
        if p:
            print('Mode: %s | %s' % (p['operating_mode_text'], p['precharge_status_text']))
            print('Alarms: %s' % ('; '.join(nc.alarm_texts(p)) or 'none'))
        if v:
            print('Software %s  Hardware %d' % (v['software'], v['hardware']))
        if telemetry.bad_frames:
            print('Ignored %d frames with a length other than 8 bytes' % telemetry.bad_frames)


# --------------------------------------------------------------------------- scan

def venus_version():
    """Venus OS version as (major, minor, text), or None when not on a GX device."""
    try:
        with open(VERSION_FILE) as f:
            text = f.readline().strip()
    except OSError:
        return None
    m = re.match(r'v?(\d+)\.(\d+)', text)
    return (int(m.group(1)), int(m.group(2)), text) if m else (0, 0, text)


def can_link_info(name):
    """(bitrate in bit/s, CAN state) of an interface from iproute2, or (None, None) if unknown."""
    try:
        out = subprocess.run(['ip', '-details', '-json', 'link', 'show', name],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
        info = json.loads(out)[0]['linkinfo']['info_data']
        return info.get('bittiming', {}).get('bitrate'), info.get('state')
    except (OSError, ValueError, LookupError, TypeError, subprocess.SubprocessError):
        return None, None


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def run_scan(configured, seconds=3.0):
    """Listen-only check of every CAN interface; reports where the converter is talking."""
    version = venus_version()
    if version:
        ok = version[:2] >= (3, 80)
        print('Venus OS %s %s' % (version[2], 'OK' if ok else
              '- the GX screen and VRM need v3.80 or newer (Settings > General > Firmware)'))
    net = SYS_NET
    names = []
    for name in sorted(os.listdir(net)):
        if _read_int(os.path.join(net, name, 'type')) == 280:   # ARPHRD_CAN
            names.append(name)
    if not names:
        print('No CAN ports found.')
        return
    print('Listening %g s on each CAN port (nothing is transmitted)...' % seconds)
    found, wrong_speed = [], []
    for name in names:
        bitrate, can_state = can_link_info(name)
        speed = '%d kbit/s' % (bitrate // 1000) if bitrate else 'speed ?'
        errors_path = os.path.join(net, name, 'statistics', 'rx_errors')
        errors_before = _read_int(errors_path)
        counts = dict.fromkeys(nc.RX_IDS, 0)
        try:
            bus = CanBus(name)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                frame = bus.recv()
                if frame:
                    counts[frame[0]] += 1
            bus.close()
        except OSError as e:
            print('  %-7s cannot listen: %s' % (name, e))
            continue
        errors = None
        if errors_before is not None:
            errors_after = _read_int(errors_path)
            if errors_after is not None:
                errors = errors_after - errors_before
        heard = sum(counts.values())
        if heard:
            found.append(name)
            result = 'CONVERTER FOUND (%d frames)' % heard
        elif errors and errors >= 10:
            # Frames that can't be decoded at this bitrate show up as bus errors.
            wrong_speed.append((name, bitrate))
            result = 'nothing decoded, but %d bus errors: talking at a different speed?' % errors
        else:
            result = 'nothing from the converter'
        state = can_state or _read_text(os.path.join(net, name, 'operstate')) or '?'
        print('  %-7s %-11s %-13s %s' % (name, speed, state.lower(), result))
    if len(found) == 1:
        if configured == AUTO:
            print('The converter is on %s. config.ini has interface = auto, so the driver finds it '
                  'there by itself. Good.' % found[0])
        elif found[0] == configured:
            print('The converter is on %s, which matches config.ini. Good.' % found[0])
        else:
            print('The converter is on %s. In config.ini set:  interface = %s' % (found[0], found[0]))
    elif not found and wrong_speed:
        name, bitrate = wrong_speed[0]
        other = {250000: '500', 500000: '250'}.get(bitrate, 'other')
        profile = {'250': 'VE.Can (250 kbit/s)', '500': 'CAN-bus BMS LV (500 kbit/s)'}.get(
            other, 'the other speed')
        print('Something is talking on %s at a different speed. On the Cerbo, set that port\'s CAN-bus '
              'profile to "%s" and run this again.' % (name, profile))
    elif not found:
        print('The converter was not heard on any port. Check that the D1 has KL30 and KL15 (12 V), '
              'the cable, the 120 ohm terminators, and try the other speed (250 or 500 kbit/s).')


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Nomadic D1 DC-DC converter monitor for Venus OS')
    ap.add_argument('--config', default=os.path.join(BASE_DIR, 'config.ini'))
    ap.add_argument('--interface', help='CAN interface, overrides [can] interface in the config')
    ap.add_argument('--monitor', action='store_true',
                    help='print decoded values in the terminal')
    ap.add_argument('--scan', action='store_true',
                    help='listen on every CAN port and report which one the converter is on')
    ap.add_argument('--simulate', action='store_true',
                    help='run against a pretend D1; no CAN bus is used at all')
    ap.add_argument('--no-dbus', action='store_true', help='run without D-Bus (web page only)')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(levelname)s: %(message)s')
    cfg = load_config(args.config)
    if args.interface:
        cfg['can']['interface'] = args.interface
    interface = cfg['can']['interface']
    timeout = cfg['can'].getfloat('timeout')

    if args.scan:
        run_scan(interface)
        return
    if args.monitor:
        run_monitor(interface, timeout)
        return

    ui_file, totals_file = 'ui.json', 'totals.json'
    if args.simulate:
        simulation_config(cfg)
        interface = SimulatedD1.interface
        ui_file, totals_file = 'ui-sim.json', 'totals-sim.json'
        log.warning('SIMULATION: pretend converter, no CAN bus is used. Ctrl+C to stop.')

    log.info('Nomadic DC-DC monitor %s starting on %s (monitor only: nothing is transmitted)',
             VERSION, interface)
    telemetry = Telemetry(timeout)
    history = History(telemetry)
    totals = Totals(os.path.join(BASE_DIR, totals_file))

    def stop(*_):   # svc -t / -d, reboot, Ctrl+C: keep the records, then go
        totals.save()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watcher = StatusWatcher(interface)

    dbus_service = None
    temperature_watch = TemperatureWatch(cfg)
    temperature_unit = TemperatureUnit(cfg['web']['temperature_unit'],
                                       None if args.no_dbus else read_gx_setting)
    if not args.no_dbus:
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)
        dbus_service = DbusService(cfg, UiSettings(os.path.join(BASE_DIR, ui_file)), temperature_watch,
                                   totals)

    if args.simulate:
        bus = SimulatedD1()
    elif interface == AUTO:
        bus = AutoCanBus(cfg)
    else:
        bus = CanBus(interface)
    watcher.bus = bus
    start_thread('can-rx', rx_worker, bus, telemetry)
    if cfg['web'].getint('port'):
        start_web(cfg, WebApp(cfg, telemetry, history, temperature_unit, temperature_watch, totals))

    def tick():
        snap = telemetry.snapshot()
        watcher(snap)
        history.tick()
        temperature_unit.poll()
        level = temperature_watch.update(snap)
        totals.update(snap, telemetry.take_energy(), level)
        if dbus_service is not None:
            dbus_service.refresh(snap)

    if dbus_service is None:
        while True:
            time.sleep(1)
            tick()

    from gi.repository import GLib

    def glib_tick():
        try:
            tick()
        except Exception:
            log.exception('D-Bus update failed')
        return True

    glib_tick()
    GLib.timeout_add(int(cfg['dbus'].getfloat('update_interval') * 1000), glib_tick)
    GLib.MainLoop().run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
