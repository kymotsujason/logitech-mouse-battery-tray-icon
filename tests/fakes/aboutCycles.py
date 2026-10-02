import json
import os
import sys

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo)
sys.path.insert(0, os.path.join(repo, "src"))

from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QMessageBox

import tray

# opens and closes the About box a few times the way a user does, and prints the most buttons any shown window had each time
app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)
bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "aboutCyclesBus")
mouseApp = tray.MouseBatteryApp(app, bus, bus)
helper = False
counts = []
for cycle in range(3):
    mouseApp.showAbout()
    QTest.qWait(200)
    box = mouseApp.aboutBox
    # a platform theme with its own message dialog shows that one, and Qt keeps the box itself off screen
    helper = helper or box.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    counts.append(max(len([button for button in widget.findChildren(QAbstractButton) if button.isVisible()]) for widget in app.topLevelWidgets() if widget.isVisible()))
    box.button(QMessageBox.StandardButton.Ok).click()
    QTest.qWait(100)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
print(json.dumps({"helper": helper, "counts": counts}))
