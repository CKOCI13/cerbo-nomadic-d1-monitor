#!/bin/sh
# Installs the service after a USB-stick / SD-card install.
#
# At boot, Venus OS looks for venus-data.tar.gz (or .tgz / .zip) on any USB stick or SD card,
# unpacks it into /data, and then runs rc/post-hook.sh from the archive with "success" or
# "extraction-failed" as its argument. The rc/ folder itself is not copied to /data.
# Source: update-data.sh in Victron's meta-victronenergy (meta-venus/recipes-core/initscripts).
#
# This runs early in the boot. install.sh adds itself to /data/rc.local, which runs again later
# in the same boot, so the service starts on this boot even if /service isn't ready yet.
[ "$1" = "success" ] || exit 0
sh /data/dbus-nomadic-dcdc/install.sh
