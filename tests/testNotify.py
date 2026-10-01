import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtWidgets import QApplication

import notify

app = QApplication.instance() or QApplication([])


class NotifierTests(unittest.TestCase):
    def testMissingServiceSkipsTheNotificationAndSaysWhy(self):
        bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "notifyTestBus")
        self.assertEqual(notify.Notifier(bus).send("Title", "Body"), (None, "org.freedesktop.DBus.Error.Disconnected"))
        QDBusConnection.disconnectFromBus("notifyTestBus")


if (__name__ == "__main__"):
    unittest.main()
