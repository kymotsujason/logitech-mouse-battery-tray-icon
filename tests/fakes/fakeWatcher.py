import sys
import time

from gi.repository import Gio, GLib

XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
    <method name="RegisterStatusNotifierHost"><arg name="service" type="s" direction="in"/></method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    <signal name="StatusNotifierItemUnregistered"><arg type="s"/></signal>
    <signal name="StatusNotifierHostRegistered"/>
    <signal name="StatusNotifierHostUnregistered"/>
  </interface>
</node>
"""
ITEM_INTERFACE = "org.kde.StatusNotifierItem"

address = sys.argv[1]
label = sys.argv[2]
late = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
startedAt = time.monotonic()
connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
items = []


def log(text):
    print(time.strftime("%H:%M:%S") + " " + label + " " + text, flush=True)


def fillPixel(service, path):
    try:
        result = connection.call_sync(service, path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (ITEM_INTERFACE, "IconPixmap")), GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return "unreadable"
    for width, height, data in result.unpack()[0]:
        if (width == 22):
            # at 81% the pixel at (11, 19) is fill, drawn in the theme's text color, and the data is ARGB in network order
            i = (19 * 22 + 11) * 4
            return "%d,%d,%d" % (data[i + 1], data[i + 2], data[i + 3])
    return "missing"


def onNewIcon(conn, sender, path, interface, signal, params, service):
    log("newIcon " + service + " fill " + fillPixel(service, path))


def onCall(conn, sender, path, interface, method, params, invocation):
    if (method != "RegisterStatusNotifierItem"):
        invocation.return_value(None)
        return
    service = params.unpack()[0]
    itemPath = "/StatusNotifierItem"
    if (service.startswith("/")):
        itemPath = service
        service = sender
    items.append(service + itemPath)
    invocation.return_value(None)
    log("registered " + service + " fill " + fillPixel(service, itemPath))
    # KStatusNotifierItem publishes the item from its own bus connection, so listen to that name and not the caller
    conn.signal_subscribe(service, ITEM_INTERFACE, "NewIcon", itemPath, None, Gio.DBusSignalFlags.NONE, onNewIcon, service)


def onGet(conn, sender, path, interface, name):
    if (name == "RegisteredStatusNotifierItems"):
        return GLib.Variant("as", items)
    if (name == "IsStatusNotifierHostRegistered"):
        return GLib.Variant("b", time.monotonic() - startedAt >= late)
    return GLib.Variant("i", 0)


def registerObject(path, interface, onMethod, onProperty):
    # Ubuntu 24.04 and Debian 12 ship a PyGObject without register_object_with_closures2
    newer = getattr(connection, "register_object_with_closures2", None)
    if (newer is not None):
        return newer(path, interface, onMethod, onProperty, None)
    return connection.register_object(path, interface, onMethod, onProperty, None)


def announceHost():
    connection.emit_signal(None, "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher", "StatusNotifierHostRegistered", None)
    # a different word from the item log lines keeps a grep for registered items from counting this
    log("host is up")
    return False


node = Gio.DBusNodeInfo.new_for_xml(XML)
registerObject("/StatusNotifierWatcher", node.interfaces[0], onCall, onGet)
Gio.bus_own_name_on_connection(connection, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, lambda *args: log("owns the watcher name"), lambda *args: log("lost the watcher name"))
if (late > 0):
    GLib.timeout_add(int(late * 1000), announceHost)
GLib.MainLoop().run()
