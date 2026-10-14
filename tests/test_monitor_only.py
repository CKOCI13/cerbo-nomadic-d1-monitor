"""The monitor must never be able to send anything to the converter.

These tests look at the code itself, so that a later change can't quietly add a way to transmit.
"""

import ast
import os
import unittest

from support import driver, nc, DRIVER_DIR


class MonitorOnlyTest(unittest.TestCase):
    def test_the_can_bus_class_cannot_send(self):
        for name in ('send', 'open_tx', 'write'):
            self.assertFalse(hasattr(driver.CanBus, name), name)

    def test_no_control_code_left(self):
        for name in ('Controller', 'tx_worker', 'TxStats', 'OUTPUTS'):
            self.assertFalse(hasattr(driver, name), name)
        self.assertFalse(hasattr(nc, 'encode_ctrl'))

    def test_no_socket_send_calls_anywhere_in_the_driver_folder(self):
        # The only "send" allowed is the web server's HTTP response (BaseHTTPRequestHandler).
        allowed = {'send_response', 'send_header', '_send'}
        for fname in sorted(os.listdir(DRIVER_DIR)):
            if not fname.endswith('.py'):
                continue
            with open(os.path.join(DRIVER_DIR, fname)) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr.startswith(('send', 'write', '_send')):
                    if node.attr in allowed or (node.attr == 'write' and _is_wfile(node)):
                        continue
                    self.fail('%s line %d uses .%s' % (fname, node.lineno, node.attr))


def _is_wfile(node):
    """self.wfile.write(...): the HTTP response body."""
    return isinstance(node.value, ast.Attribute) and node.value.attr == 'wfile'


if __name__ == '__main__':
    unittest.main()
