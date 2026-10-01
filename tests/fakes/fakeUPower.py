import sys

from gi.repository import Gio, GLib

XML = """
<node>
  <interface name="org.freedesktop.UPower">
    <method name="EnumerateDevices"><arg type="ao" direction="out"/></method>
    <signal name="DeviceAdded"><arg type="o"/></signal>
    <signal name="DeviceRemoved"><arg type="o"/></signal>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
</node>
"""
MOUSE_PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"

address = sys.argv[1]
percentFile = sys.argv[2]
connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


def log(text):
    print(text, flush=True)


def readPercent():
    try:
        with open(percentFile) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 55.0


state = {"percent": readPercent()}


def properties():
    return {"Type": GLib.Variant("u", 5), "Vendor": GLib.Variant("s", "Logitech"), "Model": GLib.Variant("s", "MX Master 3"), "Serial": GLib.Variant("s", "12-ab-34-cd"), "Percentage": GLib.Variant("d", state["percent"]), "State": GLib.Variant("u", 2), "BatteryLevel": GLib.Variant("u", 1)}


def onCall(conn, sender, path, interface, method, params, invocation):
    if (method == "EnumerateDevices"):
        invocation.return_value(GLib.Variant("(ao)", ([MOUSE_PATH],)))
    else:
        invocation.return_value(GLib.Variant("(a{sv})", (properties(),)))


def poll():
    percent = readPercent()
    if (percent != state["percent"]):
        state["percent"] = percent
        connection.emit_signal(None, MOUSE_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged", GLib.Variant("(sa{sv}as)", ("org.freedesktop.UPower.Device", {"Percentage": GLib.Variant("d", percent)}, [])))
        log("percentage " + str(percent))
    return True


def registerObject(path, interface, onMethod):
    newer = getattr(connection, "register_object_with_closures2", None)
    if (newer is not None):
        return newer(path, interface, onMethod, None, None)
    return connection.register_object(path, interface, onMethod, None, None)


node = Gio.DBusNodeInfo.new_for_xml(XML)
registerObject("/org/freedesktop/UPower", node.interfaces[0], onCall)
registerObject(MOUSE_PATH, node.interfaces[1], onCall)
Gio.bus_own_name_on_connection(connection, "org.freedesktop.UPower", Gio.BusNameOwnerFlags.NONE, lambda *args: log("owns the upower name"), None)
GLib.timeout_add(200, poll)
GLib.MainLoop().run()
