import os
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusObjectPath, QDBusPendingCall, QDBusVariant
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import dbusCalls
from tests.fakes import privateBus

app = QApplication.instance() or QApplication([])
UPOWER = "org.freedesktop.UPower"
MOUSE_PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"


def waitFor(condition, timeout=3.0):
    for i in range(int(timeout * 50)):
        if (condition()):
            return True
        QTest.qWait(20)
    return condition()


def getAll():
    message = QDBusMessage.createMethodCall(UPOWER, MOUSE_PATH, "org.freedesktop.DBus.Properties", "GetAll")
    message.setArguments(["org.freedesktop.UPower.Device"])
    return message


class DbusCallsTests(unittest.TestCase):
    def testPlainUnwrapsNestedVariants(self):
        self.assertEqual(dbusCalls.plain(QDBusVariant(QDBusVariant(5))), 5)

    def testPathTextTakesEitherForm(self):
        self.assertEqual(dbusCalls.pathText(QDBusObjectPath("/a/b")), "/a/b")
        self.assertEqual(dbusCalls.pathText("/a/b"), "/a/b")
        self.assertEqual(dbusCalls.pathText(QDBusVariant(QDBusObjectPath("/a/b"))), "/a/b")

    def testFailureGivesTheErrorNameOrNone(self):
        call = QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M")
        self.assertIsNone(dbusCalls.failure(call.createReply([1])))
        self.assertEqual(dbusCalls.failure(call.createErrorReply("org.freedesktop.DBus.Error.NoReply", "late")), "org.freedesktop.DBus.Error.NoReply")

    def testCallPassesTheTimeout(self):
        calls = []

        class Bus:
            def call(self, message, mode, timeout):
                calls.append(timeout)
                return message.createReply([])

        dbusCalls.call(Bus(), QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M"))
        dbusCalls.call(Bus(), QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M"), 5000)
        self.assertEqual(calls, [2000, 5000])


class CallLaterTests(unittest.TestCase):
    def setUp(self):
        self.owner = QObject()
        self.replies = []

    def testACallThatFinishedAtOnceStillAnswersFromTheEventLoop(self):
        dead = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "callLaterDeadBus")
        self.addCleanup(QDBusConnection.disconnectFromBus, "callLaterDeadBus")
        dbusCalls.callLater(dead, getAll(), self.replies.append, self.owner)
        self.assertEqual(self.replies, [])
        self.assertTrue(waitFor(lambda: self.replies))
        QTest.qWait(50)
        self.assertEqual([dbusCalls.failure(reply) for reply in self.replies], ["org.freedesktop.DBus.Error.Disconnected"])

    def testACompletedCallFromAFakeBusIsHandedOver(self):
        class Bus:
            def __init__(self):
                self.timeouts = []

            def asyncCall(self, message, timeout):
                self.timeouts.append(timeout)
                return QDBusPendingCall.fromCompletedCall(message.createReply([7]))

        bus = Bus()
        dbusCalls.callLater(bus, getAll(), self.replies.append, self.owner, 5000)
        self.assertTrue(waitFor(lambda: self.replies))
        self.assertEqual((bus.timeouts, self.replies[0].arguments()), ([5000], [7]))

    def testAnOwnerThatsGoneGetsNoAnswer(self):
        dead = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "callLaterGoneBus")
        self.addCleanup(QDBusConnection.disconnectFromBus, "callLaterGoneBus")
        dbusCalls.callLater(dead, getAll(), self.replies.append, self.owner)
        sip.delete(self.owner)
        QTest.qWait(100)
        self.assertEqual(self.replies, [])


class CallLaterBusTests(unittest.TestCase):
    def setUp(self):
        privateBus.requireTools(self)
        self.bus = privateBus.PrivateBus()
        self.addCleanup(self.bus.close)
        self.log = self.bus.startFake("fakeUPower.py", os.path.join(self.bus.folder, "percent"))
        self.assertTrue(self.bus.waitForLine(self.log, "owns the upower name"))
        self.connection = QDBusConnection.connectToBus(self.bus.address, "callLaterPrivateBus")
        self.addCleanup(QDBusConnection.disconnectFromBus, "callLaterPrivateBus")
        self.owner = QObject()
        self.replies = []

    def testAnAnswerArrivesThroughTheCallback(self):
        dbusCalls.callLater(self.connection, getAll(), self.replies.append, self.owner)
        self.assertTrue(waitFor(lambda: self.replies))
        self.assertIsNone(dbusCalls.failure(self.replies[0]))
        self.assertEqual(dbusCalls.plain(self.replies[0].arguments()[0])["Model"], "MX Master 3")
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.assertEqual(self.owner.children(), [])

    def testAHungPeerLeavesTheEventLoopRunningUntilItsTimeout(self):
        control = QDBusMessage.createMethodCall(UPOWER, "/org/freedesktop/UPower", "test.FakeUPower", "SetHanging")
        control.setArguments([True])
        self.assertIsNone(dbusCalls.failure(dbusCalls.call(self.connection, control)))
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        started = time.monotonic()
        dbusCalls.callLater(self.connection, getAll(), self.replies.append, self.owner, 500)
        returned = time.monotonic() - started
        self.assertTrue(waitFor(lambda: self.replies))
        timer.stop()
        self.assertLess(returned, 0.1)
        self.assertEqual(dbusCalls.failure(self.replies[0]), "org.freedesktop.DBus.Error.NoReply")
        self.assertGreater(len(ticks), 20)


if (__name__ == "__main__"):
    unittest.main()
