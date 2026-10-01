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
  <interface name="test.FakeUPower">
    <method name="SetPresent"><arg type="b" direction="in"/></method>
    <method name="FailNextGetAll"><arg type="s" direction="in"/></method>
  </interface>
</node>
"""
ROOT_PATH = "/org/freedesktop/UPower"
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


state = {"percent": readPercent(), "present": True, "failNext": None}


def properties():
    return {"Type": GLib.Variant("u", 5), "Vendor": GLib.Variant("s", "Logitech"), "Model": GLib.Variant("s", "MX Master 3"), "Serial": GLib.Variant("s", "12-ab-34-cd"), "Percentage": GLib.Variant("d", state["percent"]), "State": GLib.Variant("u", 2), "BatteryLevel": GLib.Variant("u", 1)}


def onCall(conn, sender, path, interface, method, params, invocation):
    if (interface == "test.FakeUPower"):
        if (method == "SetPresent"):
            present = params.unpack()[0]
            if (present != state["present"]):
                state["present"] = present
                connection.emit_signal(None, ROOT_PATH, "org.freedesktop.UPower", "DeviceAdded" if present else "DeviceRemoved", GLib.Variant("(o)", (MOUSE_PATH,)))
                log("present " + str(present))
        elif (method == "FailNextGetAll"):
            state["failNext"] = params.unpack()[0]
        invocation.return_value(None)
        return
    if (method == "EnumerateDevices"):
        invocation.return_value(GLib.Variant("(ao)", ([MOUSE_PATH] if state["present"] else [],)))
        return
    if (state["failNext"] is not None):
        error = state["failNext"]
        state["failNext"] = None
        invocation.return_dbus_error(error, "failed on purpose")
        log("failed GetAll with " + error)
        return
    if (not state["present"]):
        invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "no such device")
        return
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
registerObject(ROOT_PATH, node.interfaces[0], onCall)
registerObject(ROOT_PATH, node.interfaces[2], onCall)
registerObject(MOUSE_PATH, node.interfaces[1], onCall)
Gio.bus_own_name_on_connection(connection, "org.freedesktop.UPower", Gio.BusNameOwnerFlags.NONE, lambda *args: log("owns the upower name"), None)
GLib.timeout_add(200, poll)
GLib.MainLoop().run()
