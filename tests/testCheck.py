import io
import json
import os
import signal
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

import check
import hidpp
import mice
import upower

app = QApplication.instance() or QApplication([])
NAME = "PRO X3 SUPERSTRIKE"
NODE = "/dev/hidraw18"
LAST_READ = 1790000000.0
MOUSE = {"key": "02bc524c", "pathKey": False, "name": NAME, "source": NODE, "percent": 81, "charging": 0, "millivolts": None, "level": None, "asleep": False, "lastRead": LAST_READ, "wokeAt": LAST_READ - 1000}
QUIET = "No mouse answered. Move your mouse to wake it, then run this again.\n"
SEE_WHY = "  Run \"systemctl status logitech-mouse-battery\" to see why.\n"


def unreachable(error):
    return "Can't reach the battery service.\n  " + error + ": failed\n" + SEE_WHY


def stateText(entries=(), nodes=(NODE,), denied=(), busy=False, searching=False):
    return json.dumps({"version": 1, "busy": busy, "searching": searching, "nodes": list(nodes), "deniedPaths": list(denied), "mice": list(entries)})


def mouseWith(**changes):
    entry = dict(MOUSE)
    entry.update(changes)
    return entry


class FakeServiceBus:
    # answers check's blocking calls the way the service would, one answer per GetState with the last one repeating
    def __init__(self, *answers, readAllError=None):
        self.answers = list(answers)
        self.readAllError = readAllError
        self.calls = []

    def call(self, message, mode, timeout):
        self.calls.append((message.member(), timeout))
        if (message.member() == "ReadAll"):
            if (self.readAllError is not None):
                return message.createErrorReply(self.readAllError, "failed")
            return message.createReply()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if (answer.startswith("org.")):
            return message.createErrorReply(answer, "failed")
        return message.createReply([answer])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(upower, "listMice", return_value=([], [])),
            mock.patch.object(check, "POLL_MS", 1),
            mock.patch.object(check, "WAIT_SECONDS", 0.2),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def runCheck(self, bus):
        output = io.StringIO()
        code = 0
        with redirect_stdout(output):
            try:
                check.main(bus)
            except SystemExit as error:
                code = error.code
        return (code, output.getvalue())

    def withUPower(self, percent=55, key="12ab34cd", name="MX Master 3"):
        found = [upower.UPowerMouse("/p", key, name, hidpp.BatteryReading(percent, 0))]
        return mock.patch.object(upower, "listMice", return_value=(found, []))

    def testANameATerminalCantShowStillPrints(self):
        # a terminal whose locale isn't UTF-8 can take less than a mouse name holds
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with self.withUPower(name="Souris s\u00e9rie"), redirect_stdout(output):
            check.main(FakeServiceBus(stateText([])))
        output.flush()
        self.assertIn(b"Souris s?rie", raw.getvalue())

    def testPrintsTheMouseWithItsNode(self):
        self.assertEqual(self.runCheck(FakeServiceBus(stateText([MOUSE]))), (0, NAME + " (" + NODE + ")\n  81%, discharging\n"))

    def testReadAllGoesFirstWithTheLongTimeout(self):
        bus = FakeServiceBus(stateText([MOUSE]))
        self.runCheck(bus)
        self.assertEqual(bus.calls[:2], [("ReadAll", 25000), ("GetState", check.dbusCalls.DEFAULT_TIMEOUT_MS)])

    def testItWaitsUntilTheServiceIsntBusy(self):
        bus = FakeServiceBus(stateText([mouseWith(percent=70)], busy=True), stateText([MOUSE]))
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\n"))
        self.assertEqual([member for member, timeout in bus.calls].count("GetState"), 2)

    def testAStateStillBusyAtTheDeadlineIsPrintedWithAWord(self):
        bus = FakeServiceBus(stateText([MOUSE], busy=True))
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\nSome mice were still being read, so their readings may be old.\n"))

    def testAnAsleepMousePrintsWhenItWasRead(self):
        code, output = self.runCheck(FakeServiceBus(stateText([mouseWith(asleep=True)])))
        self.assertEqual((code, output), (0, NAME + " (" + NODE + ")\n  81%, discharging, asleep, read " + mice.readingTime(LAST_READ, time.time()) + "\n"))

    def testAMouseWithNoReadingYetSaysSo(self):
        entry = mouseWith(percent=None, charging=None, lastRead=None, wokeAt=None)
        self.assertEqual(self.runCheck(FakeServiceBus(stateText([entry]))), (0, NAME + " (" + NODE + ")\n  no battery reply\n"))

    def testPrintsAUPowerMouse(self):
        with self.withUPower():
            self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=()))), (0, "MX Master 3 (UPower)\n  55%, discharging\n"))

    def testASleepingUPowerMouseNeverShowsUPowersPollTime(self):
        for updatedAt in (LAST_READ, None):
            with self.subTest(updatedAt=updatedAt):
                found = [upower.UPowerMouse("/p", "12ab34cd", "G703 LS", hidpp.BatteryReading(68, hidpp.CHARGING_UNKNOWN), updatedAt=updatedAt, asleep=True)]
                with mock.patch.object(upower, "listMice", return_value=(found, [])):
                    result = self.runCheck(FakeServiceBus(stateText(nodes=())))
                self.assertEqual(result, (0, "G703 LS (UPower)\n  68%, unknown, asleep\n"))

    def testOneMouseOnBothPathsShowsWhereEachLineCameFrom(self):
        with self.withUPower(80, "02bc524c", NAME):
            self.assertEqual(self.runCheck(FakeServiceBus(stateText([MOUSE]))), (0, NAME + " (" + NODE + ")\n  81%, discharging\n" + NAME + " (UPower)\n  80%, discharging\n"))

    def testCtrlCEndsTheCheckInsteadOfWaitingOnTheBlockingCall(self):
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        self.runCheck(FakeServiceBus(stateText(nodes=())))
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_DFL)

    def testNoUPowerOnTheBusPrintsNothingAboutIt(self):
        with mock.patch.object(upower, "listMice", return_value=([], ["org.freedesktop.DBus.Error.ServiceUnknown"])):
            self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=()))), (1, "No Logitech mouse found.\n"))

    def testAUPowerErrorIsPrintedOnce(self):
        with mock.patch.object(upower, "listMice", return_value=([], ["org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.NoReply"])):
            self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=()))), (1, "UPower answered org.freedesktop.DBus.Error.NoReply\nNo Logitech mouse found.\n"))

    def testNothingAtAll(self):
        self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=()))), (1, "No Logitech mouse found.\n"))

    def testAServiceStillLookingDoesntSayThereIsNoMouse(self):
        # a service whose listing hasn't worked yet isn't busy, while it can't say what's plugged in either
        self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=(), searching=True))), (1, "Looking for mice...\n"))

    def testADeniedNodeSaysToReplugTheReceiverOrCable(self):
        self.assertEqual(self.runCheck(FakeServiceBus(stateText(nodes=(), denied=(NODE,)))), (1, "No access to " + NODE + ". Unplug the receiver or cable and plug it back in.\n"))

    def testAQuietNodeSaysToWakeTheMouse(self):
        self.assertEqual(self.runCheck(FakeServiceBus(stateText())), (1, QUIET))

    def testAFailingReadAllNamesItsError(self):
        bus = FakeServiceBus(stateText([MOUSE]), readAllError="org.freedesktop.DBus.Error.AccessDenied")
        self.assertEqual(self.runCheck(bus), (1, unreachable("org.freedesktop.DBus.Error.AccessDenied")))

    def testAFailingFirstGetStateNamesItsError(self):
        bus = FakeServiceBus("org.freedesktop.DBus.Error.NoReply")
        self.assertEqual(self.runCheck(bus), (1, unreachable("org.freedesktop.DBus.Error.NoReply")))

    def testAGetStateThatFailsWhileWaitingKeepsTheLastStateAndNamesTheError(self):
        bus = FakeServiceBus(stateText([MOUSE], busy=True), "org.freedesktop.DBus.Error.NoReply")
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\n" + check.OLD_TEXT + "\n  org.freedesktop.DBus.Error.NoReply: failed\n"))

    def testAStateThatStopsDecodingWhileWaitingKeepsTheLastState(self):
        bus = FakeServiceBus(stateText([MOUSE], busy=True), "not json")
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\n" + check.OLD_TEXT + "\n  The battery service answered with a state this version can't read.\n"))

    def testAReadTheServiceCouldntPlaceStillPrintsTheState(self):
        bus = FakeServiceBus(stateText([MOUSE]), readAllError="io.github.kymotsujason.LogitechMouseBattery1.Error.UnknownCaller")
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\nThe battery service couldn't tell which account asked, so these readings may be old.\n"))

    def testAStateThatDoesntDecodeSaysSo(self):
        self.assertEqual(self.runCheck(FakeServiceBus("not json")), (1, "The battery service answered with a state this version can't read.\n" + SEE_WHY))

    def testARefusedReadPrintsTheStateAndSaysItMayBeOld(self):
        bus = FakeServiceBus(stateText([MOUSE]), readAllError="org.freedesktop.DBus.Error.LimitsExceeded")
        self.assertEqual(self.runCheck(bus), (0, NAME + " (" + NODE + ")\n  81%, discharging\nReads are limited right now, so these readings may be old.\n"))

    def testNoServiceStillListsUPowerMice(self):
        with self.withUPower():
            bus = FakeServiceBus(stateText(), readAllError="org.freedesktop.DBus.Error.ServiceUnknown")
            self.assertEqual(self.runCheck(bus), (0, "MX Master 3 (UPower)\n  55%, discharging\n" + unreachable("org.freedesktop.DBus.Error.ServiceUnknown")))


if (__name__ == "__main__"):
    unittest.main()
