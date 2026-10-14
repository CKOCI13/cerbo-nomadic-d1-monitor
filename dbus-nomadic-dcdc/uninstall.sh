#!/bin/sh
# Stops the driver and removes it from start-up. Leaves /data/dbus-nomadic-dcdc in place.
DIR=/data/dbus-nomadic-dcdc

sed -i "\#$DIR/install.sh#d" /data/rc.local 2>/dev/null
rm -f /service/dbus-nomadic-dcdc
svc -dx "$DIR/service" "$DIR/service/log" 2>/dev/null
echo "Uninstalled. You can now delete $DIR"
