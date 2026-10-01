import os
import sys

from gi.repository import Gio, GLib

# the names are written out here instead of imported, so a wrong name in serviceState.py fails the tray's tests
SERVICE_NAME = "io.github.kymotsujason.LogitechMouseBattery"
OBJECT_PATH = "/io/github/kymotsujason/LogitechMouseBattery"
INTERFACE = "io.github.kymotsujason.LogitechMouseBattery1"
XML = """
<node>
  <interface name="io.github.kymotsujason.LogitechMouseBattery1">
    <method name="GetState"><arg type="s" direction="out"/></method>
    <method name="ReadAll"/>
    <signal name="StateChanged"><arg type="s"/></signal>
  </interface>
</node>
"""

address = os.environ.get("DBUS_STARTER_ADDRESS", "") if sys.argv[1] == "starter" else sys.argv[1]
stateFile = sys.argv[2]
startsFile = sys.argv[3] if len(sys.argv) > 3 else None
if (startsFile is not None):
    # the retry test counts the starts by pid, and the first two fail before taking the name
    with open(startsFile, "a") as f:
        f.write(str(os.getpid()) + "\n")
    with open(startsFile) as f:
        if (len(f.read().splitlines()) < 3):
            sys.exit(1)


def log(text):
    print(text, flush=True)


def readState():
    try:
        with open(stateFile) as f:
            return f.read()
    except OSError:
        return ""


state = {"text": readState()}
connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
loop = GLib.MainLoop()
# an activated copy isn't in PrivateBus's list, so it ends itself when the bus goes away
connection.connect("closed", lambda *args: loop.quit())


def onCall(conn, sender, path, interface, method, params, invocation):
    log(method)
    if (method == "GetState"):
        invocation.return_value(GLib.Variant("(s)", (readState(),)))
        return
    invocation.return_value(None)


def poll():
    text = readState()
    if (text != state["text"]):
        state["text"] = text
        connection.emit_signal(None, OBJECT_PATH, INTERFACE, "StateChanged", GLib.Variant("(s)", (text,)))
        log("sent StateChanged")
    return True


def registerObject(path, interface, onMethod):
    newer = getattr(connection, "register_object_with_closures2", None)
    if (newer is not None):
        return newer(path, interface, onMethod, None, None)
    return connection.register_object(path, interface, onMethod, None, None)


registerObject(OBJECT_PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], onCall)
Gio.bus_own_name_on_connection(connection, SERVICE_NAME, Gio.BusNameOwnerFlags.NONE, lambda *args: log("owns the service name"), None)
GLib.timeout_add(50, poll)
loop.run()
