#!/bin/sh
# Installs (or re-installs after a firmware update) the Nomadic DC-DC driver as a
# Venus OS service. Run once by hand; it adds itself to /data/rc.local for later boots.
# (The NOMADIC_* variables only exist so the automated tests can run this outside a Cerbo.)
DIR=${NOMADIC_DIR:-/data/dbus-nomadic-dcdc}
RC=${NOMADIC_RC:-/data/rc.local}
SERVICE=${NOMADIC_SERVICE:-/service}/dbus-nomadic-dcdc
LINE="$DIR/install.sh"

chmod 755 "$DIR/dbus-nomadic-dcdc.py" "$DIR/install.sh" "$DIR/uninstall.sh" \
    "$DIR/service/run" "$DIR/service/log/run"
# A file made with Windows' tar (USB install) marks everything writable by everyone
chmod -R go-w "$DIR"

# /service is rebuilt on every boot and firmware update, so this runs from rc.local
ln -sfn "$DIR/service" "$SERVICE"

# Add this script to rc.local, so it runs at every boot. Venus OS only runs rc.local when it
# is executable, and other add-ons often end it with "exit 0": a line after that never runs.
# So the line goes just before the first top-level "exit" (or at the end if there is none),
# any copy of it elsewhere is removed, and the file is always made executable.
[ -f "$RC" ] || printf '#!/bin/sh\n' > "$RC"
TMP="$RC.nomadic.tmp"
awk -v line="$LINE" '
    $0 == line { next }
    !done && /^exit([ \t]+[0-9]+)?[ \t]*$/ { print line; done = 1 }
    { print }
    END { if (!done) print line }
' "$RC" > "$TMP"
if cmp -s "$TMP" "$RC"; then
    rm -f "$TMP"
else
    # mv, not an overwrite: this can run from rc.local itself, and a shell that is still
    # reading the old file keeps reading it undisturbed.
    mv -f "$TMP" "$RC"
fi
chmod 755 "$RC"

echo "Installed. Log: tail -F /var/log/dbus-nomadic-dcdc/current | tai64nlocal"
