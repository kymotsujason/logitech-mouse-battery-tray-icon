import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path=FOLDER/bus</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
FAKES = os.path.dirname(os.path.abspath(__file__))
REQUIRE_FLAG = "MOUSE_BATTERY_REQUIRE_DBUS"


def missingTools():
    missing = []
    if (shutil.which("dbus-daemon") is None):
        missing.append("dbus-daemon")
    check = subprocess.run([sys.executable, "-c", "from gi.repository import Gio, GLib"], capture_output=True)
    if (check.returncode != 0):
        missing.append("PyGObject")
    return missing


def requireTools(testCase):
    missing = missingTools()
    if (not missing):
        return
    # CI sets the flag, so a container that lost these tools fails instead of quietly skipping the Qt 6.4 checks
    if (os.environ.get(REQUIRE_FLAG) == "1"):
        testCase.fail("missing " + ", ".join(missing) + " while " + REQUIRE_FLAG + " is 1")
    testCase.skipTest("needs " + ", ".join(missing))


def gioConnection(address):
    # PyGObject is only there where these tests run, so it's imported when a test asks for a connection
    from gi.repository import Gio
    return Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


class PrivateBus:
    def __init__(self):
        self.folder = tempfile.mkdtemp()
        config = os.path.join(self.folder, "bus.conf")
        with open(config, "w") as f:
            f.write(CONFIG.replace("FOLDER", self.folder))
        lines = subprocess.run(["dbus-daemon", "--config-file=" + config, "--fork", "--print-address=1", "--print-pid=1"], capture_output=True, text=True, check=True).stdout.splitlines()
        self.address = lines[0]
        self.pid = int(lines[1])
        self.fakes = []

    def startFake(self, script, *args):
        logPath = os.path.join(self.folder, script + ".log")
        log = open(logPath, "w")
        process = subprocess.Popen([sys.executable, os.path.join(FAKES, script), self.address, *args], stdout=log, stderr=subprocess.STDOUT)
        self.fakes.append((process, log))
        return logPath

    def lines(self, logPath):
        with open(logPath) as f:
            return f.read().splitlines()

    def waitForLine(self, logPath, line, timeout=5.0):
        deadline = time.monotonic() + timeout
        while (time.monotonic() < deadline):
            if (line in self.lines(logPath)):
                return True
            time.sleep(0.05)
        return False

    def close(self):
        for process, log in self.fakes:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        shutil.rmtree(self.folder, ignore_errors=True)
