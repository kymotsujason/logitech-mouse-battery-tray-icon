import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtWidgets import QApplication

import hidpp
import notify

app = QApplication.instance() or QApplication([])


def discharging(percent):
    return hidpp.BatteryReading(percent, 0)


class WarnerTests(unittest.TestCase):
    def setUp(self):
        self.warner = notify.LowBatteryWarner()

    def updateAndSend(self, key, reading, asleep):
        percent = self.warner.update(key, reading, asleep)
        if (percent is not None):
            self.warner.markSent(key, percent)
        return percent

    def testWarnsOnceAtTenAndOnceAtFive(self):
        results = [self.updateAndSend("m", discharging(percent), False) for percent in (50, 10, 9, 5, 4)]
        self.assertEqual(results, [None, 10, None, 5, None])

    def testAFirstReadingUnderFiveSendsOnlyOne(self):
        self.assertEqual(self.updateAndSend("m", discharging(4), False), 4)
        self.assertIsNone(self.updateAndSend("m", discharging(3), False))

    def testExternalPowerResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(9, 1), False))
        self.assertEqual(self.updateAndSend("m", discharging(9), False), 9)

    def testExternalPowerWithoutAPercentageResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 1, level="low"), False))
        self.assertEqual(self.updateAndSend("m", discharging(8), False), 8)

    def testDischargingWithoutAPercentageNeitherWarnsNorResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 0, level="low"), False))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testExternalPowerWhileAsleepDoesntReset(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 1), True))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testErrorPendingAndUnknownNeitherWarnNorReset(self):
        self.updateAndSend("m", discharging(9), False)
        for code in (4, 5, 6):
            self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(3, code), False))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testOnlyADischargingReadingAboveTwentyArmsBothWarningsAgain(self):
        results = [self.updateAndSend("m", discharging(percent), False) for percent in (10, 5, 20, 10, 5, 21, 10, 5)]
        self.assertEqual(results, [10, 5, None, None, None, None, 10, 5])

    def testAWarningStaysDueUntilItsMarkedSent(self):
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)
        self.warner.markSent("m", 9)
        self.assertIsNone(self.warner.update("m", discharging(8), False))

    def testAsleepNeverWarns(self):
        self.assertIsNone(self.updateAndSend("m", discharging(4), True))

    def testMiceAreTrackedApart(self):
        self.updateAndSend("a", discharging(9), False)
        self.assertEqual(self.updateAndSend("b", discharging(9), False), 9)


class NotifierTests(unittest.TestCase):
    def testMissingServiceSkipsTheNotification(self):
        bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "notifyTestBus")
        self.assertIsNone(notify.Notifier(bus).send("Title", "Body"))
        QDBusConnection.disconnectFromBus("notifyTestBus")


if (__name__ == "__main__"):
    unittest.main()
