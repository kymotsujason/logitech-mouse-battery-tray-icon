import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

import check
import hidpp
import upower
from tests.fakes.fakeDevice import NAME, FakeNodes, mouseHandler

app = QApplication.instance() or QApplication([])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.nodes = FakeNodes()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", self.nodes.find),
            mock.patch.object(hidpp.HidppNode, "open", self.nodes.open),
            mock.patch.object(hidpp, "PING_TIMEOUT", 0.2),
            mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.1),
            mock.patch.object(upower, "listMice", return_value=([], [])),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()

    def runCheck(self):
        output = io.StringIO()
        code = 0
        with redirect_stdout(output):
            try:
                check.main()
            except SystemExit as error:
                code = error.code
        return (code, output.getvalue())

    def testPrintsTheMouseWithItsNode(self):
        self.nodes.add("/dev/hidraw18", mouseHandler())
        self.assertEqual(self.runCheck(), (0, NAME.decode() + " (/dev/hidraw18)\n  81%, discharging\n"))

    def testPrintsAUPowerMouse(self):
        found = [upower.UPowerMouse("/p", "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0))]
        with mock.patch.object(upower, "listMice", return_value=(found, [])):
            self.assertEqual(self.runCheck(), (0, "MX Master 3 (UPower)\n  55%, discharging\n"))

    def testOneMouseOnBothPathsShowsWhereEachLineCameFrom(self):
        self.nodes.add("/dev/hidraw18", mouseHandler())
        found = [upower.UPowerMouse("/p", "02bc524c", NAME.decode(), hidpp.BatteryReading(80, 0))]
        with mock.patch.object(upower, "listMice", return_value=(found, [])):
            self.assertEqual(self.runCheck(), (0, NAME.decode() + " (/dev/hidraw18)\n  81%, discharging\n" + NAME.decode() + " (UPower)\n  80%, discharging\n"))

    def testNoUPowerOnTheBusPrintsNothingAboutIt(self):
        with mock.patch.object(upower, "listMice", return_value=([], ["org.freedesktop.DBus.Error.ServiceUnknown"])):
            self.assertEqual(self.runCheck(), (1, "No Logitech mouse found.\n"))

    def testAUPowerErrorIsPrintedOnce(self):
        with mock.patch.object(upower, "listMice", return_value=([], ["org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.NoReply"])):
            self.assertEqual(self.runCheck(), (1, "UPower answered org.freedesktop.DBus.Error.NoReply\nNo Logitech mouse found.\n"))

    def testNothingAtAll(self):
        self.assertEqual(self.runCheck(), (1, "No Logitech mouse found.\n"))

    def testADeniedNodeSaysToReplugTheReceiverOrCable(self):
        self.nodes.deny("/dev/hidraw18")
        self.assertEqual(self.runCheck(), (1, "No access to /dev/hidraw18. Unplug the receiver or cable and plug it back in.\n"))

    def testAQuietNodeSaysToWakeTheMouse(self):
        self.nodes.add("/dev/hidraw18", lambda request: [])
        self.assertEqual(self.runCheck(), (1, "No mouse answered. Move your mouse to wake it, then run this again.\n"))

    def testANodeThatFailsToOpenSaysToReplug(self):
        self.nodes.add("/dev/hidraw18", mouseHandler())
        self.nodes.failOnce("/dev/hidraw18")
        self.assertEqual(self.runCheck(), (1, "Couldn't open /dev/hidraw18: [Errno 5] Input/output error: '/dev/hidraw18'\nCan't read the receiver or cable. Unplug it and plug it back in.\n"))

    def testANodeThatRaisesSomethingElseIsReportedAndSkipped(self):
        self.nodes.add("/dev/hidraw18", mouseHandler())
        with mock.patch.object(hidpp, "searchNode", side_effect=ValueError("bad reply")):
            self.assertEqual(self.runCheck(), (1, "Couldn't read /dev/hidraw18: bad reply\nCan't read the receiver or cable. Unplug it and plug it back in.\n"))


if (__name__ == "__main__"):
    unittest.main()
