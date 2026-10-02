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

# opens the About box once for each way named on the command line and closes the window on screen that way, the way a user does, then prints how many windows each opening put on screen and the most buttons any of them had
app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)
bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "aboutCyclesBus")
mouseApp = tray.MouseBatteryApp(app, bus, bus)


def onScreen():
    return [widget for widget in app.topLevelWidgets() if widget.isVisible() and not widget.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)]


def settle():
    QTest.qWait(200)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


shown = []
buttons = []
for way in sys.argv[1:]:
    mouseApp.showAbout()
    settle()
    windows = onScreen()
    shown.append(len(windows))
    buttons.append(max((len([button for button in widget.findChildren(QAbstractButton) if button.isVisible()]) for widget in windows), default=0))
    boxes = [widget for widget in windows if isinstance(widget, QMessageBox)]
    if (not boxes):
        continue
    if (way == "ok"):
        boxes[0].button(QMessageBox.StandardButton.Ok).click()
    elif (way == "escape"):
        QTest.keyClick(boxes[0], Qt.Key.Key_Escape)
    elif (way == "close"):
        # the title bar's close button and Alt+F4 both reach the window as a close event
        boxes[0].close()
    settle()
# a plain box shows whether the platform theme has a message dialog of its own, since Qt then keeps the box itself off screen
probe = QMessageBox(QMessageBox.Icon.NoIcon, "Probe", "Probe", QMessageBox.StandardButton.Ok)
probe.show()
settle()
helper = probe.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
probe.button(QMessageBox.StandardButton.Ok).click()
settle()
print(json.dumps({"helper": helper, "shown": shown, "buttons": buttons}))
