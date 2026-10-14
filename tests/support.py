"""Shared set-up for the tests: loads the driver so it can be tested on any PC.

The driver normally runs on a Victron GX device. Two things make it testable elsewhere:

* Its file name contains dashes (``dbus-nomadic-dcdc.py``, the naming style Victron uses for
  drivers), so it can't be imported with a plain ``import``. It is loaded by path instead,
  as ``support.driver``.
* It uses Victron's ``vedbus`` library, which needs the Linux D-Bus. ``tests/fakes/vedbus.py``
  is a small stand-in with the same behaviour, so the D-Bus parts can be tested too.
"""

import importlib.util
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DRIVER_DIR = os.path.join(REPO, 'dbus-nomadic-dcdc')
CONFIG = os.path.join(DRIVER_DIR, 'config.ini')

sys.path.insert(0, DRIVER_DIR)                      # for "import nomadic_can"
sys.path.insert(0, os.path.join(HERE, 'fakes'))     # the stand-in "vedbus" wins over any real one

_spec = importlib.util.spec_from_file_location(
    'dbus_nomadic_dcdc', os.path.join(DRIVER_DIR, 'dbus-nomadic-dcdc.py'))
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

import nomadic_can as nc  # noqa: E402  (needs the path set above)

# Hand-worked frames from the CAN interface spec, used across the tests.
ELECTRIC_FRAME = bytes.fromhex('44 44 E0 07 D0 53 00 00')   # 13.6 V, +20 A, 51.2 V, -5 A, 35 degC
PARAMETER_FRAME = bytes.fromhex('06 04 82 24 00 00 00 00')  # buck mode, several alarms incl. CAN loss
VERSION_FRAME = bytes.fromhex('12 35 00 00 00 00 00 00')    # software 1.2.3, hardware 5


def load_config():
    """The shipped config.ini, as the driver reads it."""
    return driver.load_config(CONFIG)


def free_port():
    """A TCP port nobody is using, for starting the web page in a test."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
