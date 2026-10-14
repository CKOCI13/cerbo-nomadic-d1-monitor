#!/bin/sh
# Builds venus-data.tar.gz: copy it to a USB stick or SD card, put that in the Cerbo and restart
# the Cerbo, and it installs itself (see "Installing from a USB stick" in README.md).
# On Windows, Cerbo Setup.bat option U does the same.
set -e
cd "$(dirname "$0")"
tar --exclude=__pycache__ --exclude='state*.json' --exclude='ui*.json' --exclude='*.tmp' \
    -czf venus-data.tar.gz dbus-nomadic-dcdc -C usb rc
echo "Made venus-data.tar.gz. Copy it to the top folder of a USB stick or SD card."
