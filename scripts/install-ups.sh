#!/bin/sh
# Install the UPS monitor as root-owned code in /opt/carcount and (re)start it if enabled.
#
# Why: carcount-ups runs as root (GPIO, I2C, poweroff). Running it straight from
# ~/src would let anything that can write as the carcount user (the web page has
# no login) get root. The root copy can only be changed with sudo.
#
# Run after the first install AND after every change to ups_monitor.py, alerts.py
# or carcount_data.py (the Power page warns when the copy is out of date):
#     sudo sh scripts/install-ups.sh            # install / update the code + unit
#     sudo systemctl enable --now carcount-ups  # first time only (batteries in + tested)
set -eu
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo sh $0" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/carcount

python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" \
    "$SRC/ups_monitor.py" "$SRC/alerts.py" "$SRC/carcount_data.py"

install -d -o root -g root -m 755 "$DEST"
for f in ups_monitor.py alerts.py carcount_data.py; do
    install -o root -g root -m 644 "$SRC/$f" "$DEST/$f"
done
rm -rf "$DEST/__pycache__"
install -o root -g root -m 644 "$SRC/systemd/carcount-ups.service" /etc/systemd/system/carcount-ups.service
systemctl daemon-reload
echo "Installed to $DEST:"
ls -l "$DEST"
if systemctl is-enabled --quiet carcount-ups; then
    systemctl restart carcount-ups
    echo "carcount-ups restarted."
else
    echo "carcount-ups is not enabled. When the batteries are tested: sudo systemctl enable --now carcount-ups"
fi
