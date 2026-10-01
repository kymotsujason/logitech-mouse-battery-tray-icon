import sys

from gi.repository import Gio, GLib

XML = """
<node>
  <interface name="org.freedesktop.Notifications">
    <method name="Notify">
      <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
      <arg type="u" direction="out"/>
    </method>
    <method name="GetCapabilities"><arg type="as" direction="out"/></method>
    <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
    <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
  </interface>
  <interface name="test.FakeNotifications">
    <method name="Press"><arg type="u" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Close"><arg type="u" direction="in"/></method>
  </interface>
</node>
"""
PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

address = sys.argv[1]
# "-" presses nothing, the way a missing argument does
pressKey = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
capabilities = sys.argv[3].split(",") if len(sys.argv) > 3 else ["actions", "body", "body-markup"]
connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
counter = [0]


def log(text):
    print(text, flush=True)


def press(notificationId, key):
    connection.emit_signal(None, PATH, INTERFACE, "ActionInvoked", GLib.Variant("(us)", (notificationId, key)))
    log("pressed " + key)
    return False


def close(notificationId):
    connection.emit_signal(None, PATH, INTERFACE, "NotificationClosed", GLib.Variant("(uu)", (notificationId, 2)))
    log("closed " + str(notificationId))


def onCall(conn, sender, path, interface, method, params, invocation):
    if (method == "GetCapabilities"):
        invocation.return_value(GLib.Variant("(as)", (capabilities,)))
        return
    if (method == "Press"):
        notificationId, key = params.unpack()
        press(notificationId, key)
        invocation.return_value(None)
        return
    if (method == "Close"):
        close(params.unpack()[0])
        invocation.return_value(None)
        return
    appName, replacesId, icon, summary, body, actions, hints, timeout = params.unpack()
    counter[0] += 1
    pairs = [actions[i] + ":" + actions[i + 1] for i in range(0, len(actions) - 1, 2)]
    log("notify " + summary + "|" + body + "|" + ",".join(pairs))
    invocation.return_value(GLib.Variant("(u)", (counter[0],)))
    if (pressKey is not None and pressKey in actions[0::2]):
        GLib.timeout_add(300, press, counter[0], pressKey)


def registerObject(path, interface, onMethod):
    newer = getattr(connection, "register_object_with_closures2", None)
    if (newer is not None):
        return newer(path, interface, onMethod, None, None)
    return connection.register_object(path, interface, onMethod, None, None)


node = Gio.DBusNodeInfo.new_for_xml(XML)
registerObject(PATH, node.interfaces[0], onCall)
registerObject(PATH, node.interfaces[1], onCall)
Gio.bus_own_name_on_connection(connection, INTERFACE, Gio.BusNameOwnerFlags.NONE, lambda *args: log("owns the notifications name"), None)
GLib.MainLoop().run()
