#!/usr/bin/env bash
# runs inside a clean container with the repo at /repo: installs the distro's own Python and PyQt6, prints their
# versions, runs the unit suite, prints its whole output on a failure, and fails when no test ran at all
set -u

if ! bash -c "$1" > /tmp/install.log 2>&1; then
    echo "The install failed:"
    tail -20 /tmp/install.log
    exit 2
fi
cp -r /repo /tmp/app
cd /tmp/app
python3 -c 'import sys; from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR; print("Python " + sys.version.split()[0] + ", Qt " + QT_VERSION_STR + ", PyQt6 " + PYQT_VERSION_STR)'
MOUSE_BATTERY_REQUIRE_DBUS=1 QT_QPA_PLATFORM=offscreen python3 -B -m unittest discover -s tests -t . > /tmp/suite.log 2>&1
status=$?
if [ "$status" -ne 0 ]; then
    cat /tmp/suite.log
    exit "$status"
fi
tail -3 /tmp/suite.log
if ! grep -q "^Ran [1-9][0-9]* tests\? in" /tmp/suite.log; then
    echo "No test ran"
    exit 3
fi
