#!/bin/sh
# Installs (or re-installs after a firmware update) the Nomadic DC-DC driver as a
# Venus OS service. Run once by hand; it adds itself to /data/rc.local for later boots.
DIR=/data/dbus-nomadic-dcdc

chmod 755 "$DIR/dbus-nomadic-dcdc.py" "$DIR/install.sh" "$DIR/uninstall.sh" \
    "$DIR/service/run" "$DIR/service/log/run"

# /service is rebuilt on every boot and firmware update, so this runs from rc.local
ln -sfn "$DIR/service" /service/dbus-nomadic-dcdc

RC=/data/rc.local
if [ ! -f "$RC" ]; then
    echo '#!/bin/sh' > "$RC"
    chmod 755 "$RC"
fi
grep -qF "$DIR/install.sh" "$RC" || echo "$DIR/install.sh" >> "$RC"

echo "Installed. Log: tail -F /var/log/dbus-nomadic-dcdc/current | tai64nlocal"
