import json
import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import dbusCalls
import notify
from tests.fakes import privateBus
from tests.fakes.recordingBus import RecordingBus

app = QApplication.instance() or QApplication([])
SRC = os.path.dirname(os.path.abspath(notify.__file__))
# the bug only shows when Notify is the first D-Bus marshaling in its process, so each send runs in a fresh one and goes out before a Notifier subscribes to anything, since that hid the bug on Qt 6.4
FIRST_SEND = """
import json
import sys
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection
app = QCoreApplication(sys.argv[:1])
import dbusCalls
import notify
pairs = [tuple(pair) for pair in json.loads(sys.argv[1])]
reply = dbusCalls.call(QDBusConnection.sessionBus(), notify.notifyMessage("Summary", "Body", pairs), notify.CALL_TIMEOUT_MS)
if (dbusCalls.failure(reply) is not None or not reply.arguments()):
    print(json.dumps([None, reply.errorName()]))
else:
    print(json.dumps([reply.arguments()[0], None]))
"""


def waitFor(condition, timeout=2.0):
    for i in range(int(timeout * 50)):
        if (condition()):
            return True
        QTest.qWait(20)
    return condition()


class NotifierTests(unittest.TestCase):
    def testMissingServiceSkipsTheNotificationAndSaysWhy(self):
        bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "notifyTestBus")
        self.assertEqual(notify.Notifier(bus).send("Title", "Body"), (None, "org.freedesktop.DBus.Error.Disconnected"))
        QDBusConnection.disconnectFromBus("notifyTestBus")

    def testSignalsAreTakenOnlyFromTheServer(self):
        bus = RecordingBus()
        notify.Notifier(bus)
        self.assertEqual(bus.subscriptions, [
            (notify.SERVICE, notify.PATH, notify.SERVICE, "ActionInvoked"),
            (notify.SERVICE, notify.PATH, notify.SERVICE, "NotificationClosed"),
        ])

    def testTheActionListIsAStringList(self):
        self.assertEqual([notify.stringListVariant(values).typeName() for values in ([], ["a", "First"])], ["QStringList", "QStringList"])

    def testABodyIsEscapedWhenTheServerCantSayWhatItSupports(self):
        self.assertEqual(notify.Notifier(RecordingBus()).bodyText("<b>A&B</b>"), "&lt;b&gt;A&amp;B&lt;/b&gt;")

    def testAReusedIdKeepsItsNewHandler(self):
        notifier = notify.Notifier(RecordingBus())
        first = mock.Mock()
        second = mock.Mock()
        notifier.handlers[7] = first
        closed = QDBusMessage.createSignal(notify.PATH, notify.SERVICE, "NotificationClosed")
        closed.setArguments([notify.uintArgument(7), notify.uintArgument(2)])
        with mock.patch.object(notify, "HANDLER_GRACE_MS", 50):
            notifier.onNotificationClosed(closed)
        notifier.handlers[7] = second
        QTest.qWait(200)
        self.assertIs(notifier.handlers.get(7), second)


class FakeServerTest(unittest.TestCase):
    capabilities = None

    def setUp(self):
        privateBus.requireTools(self)
        self.bus = privateBus.PrivateBus()
        self.addCleanup(self.bus.close)
        args = ["-"] if self.capabilities is None else ["-", self.capabilities]
        self.log = self.bus.startFake("fakeNotifications.py", *args)
        self.assertTrue(self.bus.waitForLine(self.log, "owns the notifications name"))

    def qtConnection(self, name):
        connection = QDBusConnection.connectToBus(self.bus.address, name)
        self.addCleanup(QDBusConnection.disconnectFromBus, name)
        return connection


class FirstNoticeTests(FakeServerTest):
    def firstSend(self, pairs):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.bus.address, QT_QPA_PLATFORM="offscreen", PYTHONPATH=SRC)
        result = subprocess.run([sys.executable, "-B", "-c", FIRST_SEND, json.dumps(pairs)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def testANoticeWithoutButtonsWorksAsTheFirstCallOfAProcess(self):
        notificationId, error = self.firstSend([])
        self.assertIsNone(error)
        self.assertGreater(notificationId, 0)
        self.assertTrue(self.bus.waitForLine(self.log, "notify Summary|Body|"))

    def testANoticeWithTwoButtonsWorksAsTheFirstCallOfAProcess(self):
        notificationId, error = self.firstSend([["a", "First"], ["b", "Second"]])
        self.assertIsNone(error)
        self.assertGreater(notificationId, 0)
        self.assertTrue(self.bus.waitForLine(self.log, "notify Summary|Body|a:First,b:Second"))


class MarkupServerTests(FakeServerTest):
    def testABodyIsEscapedForAServerThatParsesMarkup(self):
        notifier = notify.Notifier(self.qtConnection("notifyMarkupBus"))
        self.assertIsNone(notifier.send("S", "<b>A&B</b>")[1])
        self.assertTrue(self.bus.waitForLine(self.log, "notify S|&lt;b&gt;A&amp;B&lt;/b&gt;|"))


class PlainServerTests(FakeServerTest):
    capabilities = "actions,body"

    def testABodyStaysAsWrittenForAServerWithoutMarkup(self):
        notifier = notify.Notifier(self.qtConnection("notifyPlainBus"))
        self.assertIsNone(notifier.send("S", "<b>A&B</b>")[1])
        self.assertTrue(self.bus.waitForLine(self.log, "notify S|<b>A&B</b>|"))


class ActionTests(FakeServerTest):
    def setUp(self):
        super().setUp()
        self.notifier = notify.Notifier(self.qtConnection("notifyActionBus"))
        # a second connection drives the fake and forges signals, so the test process never imports PyGObject
        self.controlBus = self.qtConnection("notifyControlBus")
        self.pressed = []
        self.notificationId, error = self.notifier.send("S", "B", [("go", "Go")], self.pressed.append)
        self.assertIsNone(error)

    def control(self, method, notificationId, *args):
        message = QDBusMessage.createMethodCall(notify.SERVICE, notify.PATH, "test.FakeNotifications", method)
        message.setArguments([notify.uintArgument(notificationId), *args])
        self.assertIsNone(dbusCalls.failure(dbusCalls.call(self.controlBus, message)))

    def forge(self, notificationId, key):
        message = QDBusMessage.createSignal(notify.PATH, notify.SERVICE, "ActionInvoked")
        message.setArguments([notify.uintArgument(notificationId), key])
        self.assertTrue(self.controlBus.send(message))

    def testTheServersActionReachesItsHandler(self):
        self.control("Press", self.notificationId, "go")
        self.assertTrue(waitFor(lambda: self.pressed == ["go"]))

    def testAnotherIdsActionIsIgnored(self):
        self.control("Press", self.notificationId + 100, "go")
        QTest.qWait(300)
        self.assertEqual(self.pressed, [])

    def testAnActionForgedByAnotherConnectionIsIgnored(self):
        self.forge(self.notificationId, "go")
        QTest.qWait(300)
        self.assertEqual(self.pressed, [])
        # the same signal from the server still gets through, which shows the subscription is live
        self.control("Press", self.notificationId, "go")
        self.assertTrue(waitFor(lambda: self.pressed == ["go"]))

    def testClosingTheNoticeDropsItsHandler(self):
        with mock.patch.object(notify, "HANDLER_GRACE_MS", 50):
            self.control("Close", self.notificationId)
            self.assertTrue(waitFor(lambda: self.notificationId not in self.notifier.handlers))
        self.control("Press", self.notificationId, "go")
        QTest.qWait(300)
        self.assertEqual(self.pressed, [])


if (__name__ == "__main__"):
    unittest.main()
