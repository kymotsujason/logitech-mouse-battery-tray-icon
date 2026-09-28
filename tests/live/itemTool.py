import sys

from gi.repository import Gio, GLib

address, service, command = sys.argv[1:4]
connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


def itemProperty(name):
    result = connection.call_sync(service, "/StatusNotifierItem", "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", name)), GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
    return result.unpack()[0]


if (command == "tooltip"):
    # KDE's item puts the QSystemTrayIcon tooltip in the title field, and sorting keeps the order out of the comparison
    print("|".join(sorted(itemProperty("ToolTip")[2].splitlines())))
elif (command == "menuOpened"):
    connection.call_sync(service, itemProperty("Menu"), "com.canonical.dbusmenu", "AboutToShow", GLib.Variant("(i)", (0,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None)
