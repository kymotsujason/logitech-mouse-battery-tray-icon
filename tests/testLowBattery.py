import unittest

import hidpp
import lowBattery


def discharging(percent):
    return hidpp.BatteryReading(percent, 0)


class WarnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.warner = lowBattery.LowBatteryWarner(clock=lambda: self.now)

    def windowPasses(self):
        self.now += lowBattery.NOTICE_WINDOW_SECONDS

    def updateAndSend(self, key, reading, asleep):
        percent = self.warner.update(key, reading, asleep)
        if (percent is not None):
            self.warner.confirm(key, percent)
        return percent

    def testARoughLevelWarnsAtLowAndAgainAtCritical(self):
        results = [self.updateAndSend("m", hidpp.BatteryReading(None, 0, level=level), False) for level in ("good", "low", "low", "critical")]
        self.assertEqual(results, [None, 10, None, 5])

    def testAVoltageWarnsOnTheKernelsCurve(self):
        self.assertEqual(self.updateAndSend("m", hidpp.BatteryReading(None, 0, millivolts=3700), False), None)
        self.assertEqual(self.updateAndSend("m", hidpp.BatteryReading(None, 0, millivolts=3633), False), 4)

    def testAnUnknownChargingStateStillWarns(self):
        # UPower can't always say whether a mouse is charging, while a low percent still needs its warning
        self.assertEqual(self.updateAndSend("m", hidpp.BatteryReading(9, hidpp.CHARGING_UNKNOWN), False), 9)

    def testAVoltageMouseOnExternalPowerDoesntWarn(self):
        # HID++ gives an unknown state only for a voltage mouse with external power on
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, hidpp.CHARGING_UNKNOWN, millivolts=3600), False))

    def testAPendingChargeOrAChargingErrorDoesntWarn(self):
        for charging in (hidpp.CHARGING_PENDING, hidpp.CHARGING_ERROR):
            self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(9, charging), False))

    def testWarnsOnceAtTenAndOnceAtFive(self):
        results = [self.updateAndSend("m", discharging(percent), False) for percent in (50, 10, 9, 5, 4)]
        self.assertEqual(results, [None, 10, None, 5, None])

    def testAFirstReadingUnderFiveSendsOnlyOne(self):
        self.assertEqual(self.updateAndSend("m", discharging(4), False), 4)
        self.assertIsNone(self.updateAndSend("m", discharging(3), False))

    def testExternalPowerResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(9, 1), False))
        self.windowPasses()
        self.assertEqual(self.updateAndSend("m", discharging(9), False), 9)

    def testExternalPowerWithoutAPercentageResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 1, level="low"), False))
        self.windowPasses()
        self.assertEqual(self.updateAndSend("m", discharging(8), False), 8)

    def testDischargingWithoutAPercentageNeitherWarnsNorResets(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 0, level="low"), False))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testExternalPowerWhileAsleepDoesntReset(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(None, 1), True))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testErrorAndPendingNeitherWarnNorReset(self):
        self.updateAndSend("m", discharging(9), False)
        for code in (4, 5):
            self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(3, code), False))
        self.assertIsNone(self.updateAndSend("m", discharging(8), False))

    def testOnlyADischargingReadingAboveTwentyArmsBothWarningsAgain(self):
        results = [self.updateAndSend("m", discharging(percent), False) for percent in (10, 5, 20, 10, 5)]
        self.windowPasses()
        results += [self.updateAndSend("m", discharging(percent), False) for percent in (20, 10, 5, 21, 10, 5)]
        self.assertEqual(results, [10, 5, None, None, None, None, None, None, None, 10, 5])

    def testAMouseThatKeepsArmingAgainGetsEachWarningOnceAWindow(self):
        results = []
        for i in range(10):
            results.append(self.updateAndSend("m", discharging(9), False))
            self.updateAndSend("m", discharging(25), False)
        self.assertEqual(results, [9] + [None] * 9)
        self.windowPasses()
        self.assertEqual(self.updateAndSend("m", discharging(9), False), 9)

    def testFreshKeysAtALowLevelStopAtTheLimit(self):
        results = [self.updateAndSend(str(key), discharging(9), False) for key in range(20)]
        self.assertEqual(results, [9] * lowBattery.NOTICE_LIMIT + [None] * (20 - lowBattery.NOTICE_LIMIT))
        self.assertLessEqual(len(self.warner.notices), lowBattery.NOTICE_LIMIT)

    def testAWarningHeldBackByTheLimitGoesOutOnceTheWindowAllowsIt(self):
        for key in range(lowBattery.NOTICE_LIMIT):
            self.updateAndSend(str(key), discharging(9), False)
        self.assertIsNone(self.updateAndSend("late", discharging(9), False))
        self.windowPasses()
        self.assertEqual(self.updateAndSend("late", discharging(9), False), 9)

    def testForgettingAKeyDropsItsArmingButNotItsRecentWarnings(self):
        self.updateAndSend("m", discharging(9), False)
        self.warner.forget("m")
        self.assertNotIn("m", self.warner.sent)
        self.assertIsNone(self.updateAndSend("m", discharging(9), False))

    def testForgettingAPathKeyLetsTheNextMouseThereBeWarned(self):
        self.updateAndSend("/dev/hidraw4#1", discharging(9), False)
        self.warner.forget("/dev/hidraw4#1", pathKey=True)
        self.assertEqual(self.updateAndSend("/dev/hidraw4#1", discharging(9), False), 9)

    def testHighReadingsThatArentAwakeAndDischargingDontArmThemAgain(self):
        self.updateAndSend("m", discharging(9), False)
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(50, 4), False))
        self.assertIsNone(self.updateAndSend("m", hidpp.BatteryReading(50, 6), False))
        self.assertIsNone(self.updateAndSend("m", discharging(50), True))
        self.assertIsNone(self.updateAndSend("m", discharging(9), False))

    def testAWarningOnItsWayIsntHandedOutAgain(self):
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)
        self.assertIsNone(self.warner.update("m", discharging(9), False))

    def testAWarningGivenBackIsDueAgain(self):
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)
        self.warner.giveBack("m", 9)
        self.assertEqual(self.warner.update("m", discharging(8), False), 8)

    def testWarningsOnTheirWayCountAgainstTheLimit(self):
        results = [self.warner.update(str(key), discharging(9), False) for key in range(10)]
        self.assertEqual(results, [9] * lowBattery.NOTICE_LIMIT + [None] * (10 - lowBattery.NOTICE_LIMIT))

    def testAWarningGivenBackLeavesRoomUnderTheLimit(self):
        for key in range(lowBattery.NOTICE_LIMIT):
            self.warner.update(str(key), discharging(9), False)
        self.warner.giveBack("0", 9)
        self.assertEqual(self.warner.update("late", discharging(9), False), 9)

    def testTheCriticalWarningGoesOutWhileTheLowOneIsOnItsWay(self):
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)
        self.assertEqual(self.warner.update("m", discharging(4), False), 4)
        self.warner.giveBack("m", 9)
        self.warner.confirm("m", 4)
        self.assertIsNone(self.warner.update("m", discharging(4), False))

    def testAFailedNoticeForAMouseThatLeftIsGivenBack(self):
        self.warner.update("m", discharging(9), False)
        # the mouse leaves while its notice is on its way, and the notice then fails
        self.warner.forget("m")
        self.warner.giveBack("m", 9)
        self.assertEqual(self.warner.update("m", discharging(9), False), 9)

    def testAsleepNeverWarns(self):
        self.assertIsNone(self.updateAndSend("m", discharging(4), True))

    def testMiceAreTrackedApart(self):
        self.updateAndSend("a", discharging(9), False)
        self.assertEqual(self.updateAndSend("b", discharging(9), False), 9)


if (__name__ == "__main__"):
    unittest.main()
