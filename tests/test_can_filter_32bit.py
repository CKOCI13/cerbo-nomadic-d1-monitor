"""The CAN receive filter must build on 32-bit Python, as on the Cerbo GX.

On 32-bit Python, socket.CAN_EFF_FLAG is -2147483648 rather than 2147483648. The driver once built
its filter from it, and struct.pack('=I') refused the negative number, so the driver crashed as soon
as it opened a CAN port on a real Cerbo. These tests set the 32-bit values and build the filter.
"""

import socket
import struct
import unittest
from unittest import mock

from support import driver, nc


class FakeSocket:
    def __init__(self, *args):
        self.filters = None

    def setsockopt(self, level, option, value):
        if option == socket.CAN_RAW_FILTER:
            self.filters = value

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def setblocking(self, flag):
        pass

    def close(self):
        pass


class CanFilter32BitTest(unittest.TestCase):

    def build(self):
        made = []

        def factory(*args):
            s = FakeSocket(*args)
            made.append(s)
            return s

        # The values 32-bit Python gives these constants.
        with mock.patch.object(socket, 'CAN_EFF_FLAG', -0x80000000, create=True), \
                mock.patch.object(socket, 'CAN_RTR_FLAG', 0x40000000, create=True), \
                mock.patch.object(socket, 'CAN_SFF_MASK', 0x7FF, create=True), \
                mock.patch.object(driver.socket, 'socket', factory):
            driver.CanBus('vcan0')
        return made[0].filters

    def test_filter_builds_with_32bit_constants(self):
        filters = self.build()
        pairs = [struct.unpack('=II', filters[i:i + 8]) for i in range(0, len(filters), 8)]
        self.assertEqual([p[0] for p in pairs], list(nc.RX_IDS))
        for _, mask in pairs:
            self.assertEqual(mask, 0x7FF | 0x80000000 | 0x40000000)

    def test_driver_uses_its_own_constants(self):
        self.assertEqual(driver.CAN_SFF_MASK, 0x7FF)
        self.assertEqual(driver.CAN_EFF_FLAG, 0x80000000)
        self.assertEqual(driver.CAN_RTR_FLAG, 0x40000000)


if __name__ == '__main__':
    unittest.main()
