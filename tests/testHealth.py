import sys
import time
import unittest
from unittest import mock

import health
import hidpp
import history

NOW = 1800000000
DAY = 24 * 60 * 60


def bucket(top, first=None, last=None, sessions=0):
    return {"bucket": top, "sessions": sessions, "first": first, "last": last}


def readyBuckets(tops, first, last):
    return [bucket(top, first, list(last), 42) for top in tops] + [bucket(top) for top in (80, 70, 60, 50, 40, 30)[len(tops):]]


def summary(**changes):
    base = history.emptyHistory("02bc524c")
    base["wakeSupport"] = True
    base.update(changes)
    return base


def reading(percent=62, charging=0):
    return hidpp.BatteryReading(percent, charging)


class ComparisonTests(unittest.TestCase):
    def testABucketWithoutBothSidesHasNoRatios(self):
        self.assertIsNone(health.bucketRatios(bucket(80, 30.0, None)))
        self.assertIsNone(health.bucketRatios(bucket(80, None, [30.0, 30.0, 30.0])))
        self.assertEqual(health.bucketRatios(bucket(80, 30.0, [24.0, 27.0, 30.0])), [0.8, 0.9, 1.0])

    def testAComparisonNeedsThreeReadyBuckets(self):
        two = readyBuckets((80, 70), 30.0, (24.0, 24.0, 24.0))
        self.assertIsNone(health.comparison(two))
        three = readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 24.0))
        self.assertEqual(health.comparison(three), [0.8, 0.8, 0.8])

    def testTheFigureIsTheMedianOfTheReadyRatios(self):
        buckets = [bucket(80, 10.0, [8.0, 8.0, 8.0], 42), bucket(70, 10.0, [9.0, 9.0, 9.0], 42), bucket(60, 10.0, [12.0, 12.0, 12.0], 42)]
        self.assertEqual(health.comparison(buckets), [0.9, 0.9, 0.9])


class LineTests(unittest.TestCase):
    def lines(self, value, current=None):
        result = health.assess(value, current or reading(), NOW)
        return [(line.kind, line.text) for line in result.lines], result.symbol

    def testLearningShowsItsCountAndAClock(self):
        value = summary(drain={"buckets": readyBuckets((80,), 30.0, (30.0, 30.0, 30.0)), "largeSteps": False})
        self.assertEqual(self.lines(value), ([(health.LEARNING, "Still learning, 1 of 3 battery ranges measured.")], health.SYMBOL_CLOCK))

    def testAnUnchangedBatteryIsDoingWell(self):
        value = summary(drain={"buckets": readyBuckets((80, 70, 60), 30.0, (29.0, 30.0, 31.0)), "largeSteps": False})
        self.assertEqual(self.lines(value), ([(health.GOOD, health.GOOD_TEXT)], health.SYMBOL_CHECK))

    def testAShorterChargeOnAllThreeFiguresIsAWarning(self):
        value = summary(drain={"buckets": readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 24.0)), "largeSteps": False})
        lines, symbol = self.lines(value)
        self.assertEqual((lines[0][0], symbol), (health.DRAIN, health.SYMBOL_WARNING))
        self.assertIn("about 20% less", lines[0][1])

    def testOneFigureNotCrossingKeepsTheWarningOff(self):
        value = summary(drain={"buckets": readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 27.0)), "largeSteps": False})
        self.assertEqual(self.lines(value)[0], [(health.GOOD, health.GOOD_TEXT)])

    def testARecentChargingErrorIsTheHeadlineEvenWhileLearning(self):
        value = summary(lastError=NOW - DAY)
        lines, symbol = self.lines(value)
        self.assertEqual([kind for kind, text in lines], [health.ALERT, health.LEARNING])
        self.assertEqual((lines[0][1], symbol), (health.CHARGING_ERROR_TEXT, health.SYMBOL_WARNING))

    def testAnOldChargingErrorIsntShown(self):
        value = summary(lastError=NOW - 8 * DAY)
        self.assertEqual([kind for kind, text in self.lines(value)[0]], [health.LEARNING])

    def testASlowLastChargeNeedsFiveReadings(self):
        slow = {"kind": 1, "startAt": NOW, "endAt": NOW, "startPercent": 20, "endPercent": 80, "power": 1, "readingCount": 4, "slowCount": 4, "errorCount": 0, "inferred": 0}
        self.assertEqual([kind for kind, text in self.lines(summary(lastCharge=slow))[0]], [health.LEARNING])
        slow = dict(slow, readingCount=6, slowCount=4)
        self.assertEqual(self.lines(summary(lastCharge=slow))[0][0], (health.ALERT, health.CHARGING_SLOW_TEXT))

    def testOneKindChargingSlowerNamesBothCableAndBattery(self):
        value = summary(charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.6, 0.6, 0.6))}])
        lines = self.lines(value)[0]
        self.assertEqual(lines[0][0], health.CHARGE_LINE_KIND)
        self.assertIn("Try another cable or charger, and if that doesn't help", lines[0][1])
        self.assertIn("about 20% longer", lines[0][1])

    def testOneOfTwoKindsSlowerNamesThatCharger(self):
        value = summary(charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.6, 0.6, 0.6))}, {"power": 1, "buckets": readyBuckets((20, 30, 40), 0.5, (0.5, 0.5, 0.5))}])
        self.assertIn("Charging on the wireless charger takes about 20% longer", self.lines(value)[0][0][1])

    def testEveryKindSlowerBlamesTheBatteryWithTheSmallestN(self):
        value = summary(charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.6, 0.6, 0.6))}, {"power": 1, "buckets": readyBuckets((20, 30, 40), 0.5, (0.65, 0.65, 0.65))}])
        self.assertEqual(self.lines(value)[0][0][1], "Charging takes about 20% longer than it used to, so the battery may be wearing.")

    def testAMouseWithoutWakeSupportStillGetsItsChargeVerdicts(self):
        value = summary(wakeSupport=False, charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.5, 0.5, 0.5))}])
        lines, symbol = self.lines(value)
        self.assertEqual([kind for kind, text in lines], [health.GOOD, health.LEARNING])
        self.assertEqual((lines[1][1], symbol), (health.NO_WAKES_TEXT, health.SYMBOL_CHECK))

    def testTheCapabilityMessagesComeInTheirOrder(self):
        rough = hidpp.BatteryReading(None, 0, level="good")
        voltage = hidpp.BatteryReading(None, 0, millivolts=3850)
        self.assertEqual(self.lines(summary(wakeSupport=False), rough)[0], [(health.LEARNING, health.ROUGH_TEXT)])
        self.assertEqual(self.lines(summary(wakeSupport=False), voltage)[0], [(health.LEARNING, health.VOLTAGE_TEXT)])
        large = summary(drain={"buckets": readyBuckets((), 0, ()), "largeSteps": True})
        self.assertEqual(self.lines(large)[0], [(health.LEARNING, health.LARGE_STEPS_TEXT)])

    def testAMouseWhoseWakesArentVisibleYetIsntStillLearning(self):
        value = summary(wakesVisible=False, drain={"buckets": readyBuckets((80,), 30.0, (30.0, 30.0, 30.0)), "largeSteps": False})
        self.assertEqual(self.lines(value), ([(health.LEARNING, health.WAKES_UNSEEN_TEXT)], health.SYMBOL_CLOCK))
        # a service from before the field still gets its learning count
        old = summary()
        del old["wakesVisible"]
        self.assertEqual(self.lines(old)[0][0][1], "Still learning, 0 of 3 battery ranges measured.")

    def testNoMoreThanThreeLines(self):
        value = summary(lastError=NOW, drain={"buckets": readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 24.0)), "largeSteps": False}, charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.6, 0.6, 0.6))}])
        self.assertEqual([kind for kind, text in self.lines(value)[0]], [health.ALERT, health.DRAIN, health.CHARGE_LINE_KIND])


class FigureTests(unittest.TestCase):
    def testTimeLeftNeedsFiveDrops(self):
        value = summary(open={"kind": 0, "startAt": NOW, "startPercent": 100, "median": 12.0, "count": 4})
        self.assertIsNone(health.assess(value, reading(62), NOW).timeLeft)
        value["open"]["count"] = 5
        self.assertEqual(health.assess(value, reading(62), NOW).timeLeft, 744.0)

    def testTimeToFullNeedsThreeRisesAndACharge(self):
        value = summary(open={"kind": 1, "startAt": NOW, "startPercent": 20, "median": 0.4, "count": 3})
        self.assertEqual(health.assess(value, reading(45, hidpp.CHARGING_CHARGING), NOW).timeToFull, 22.0)
        self.assertIsNone(health.assess(value, reading(45, 0), NOW).timeToFull)

    def testTheStatusSuffixReadsAsASentence(self):
        self.assertEqual(health.statusSuffix(health.Health(timeLeft=840)), ", about 14 hours left")
        self.assertEqual(health.statusSuffix(health.Health(timeToFull=40)), ", full in about 40 min")
        self.assertEqual(health.statusSuffix(health.Health()), "")

    def testCyclesRoundDown(self):
        value = summary(monthly=[{"month": "2026-09", "drain": None, "charge": [], "drained": 150, "sessions": 2}, {"month": "2026-10", "drain": None, "charge": [], "drained": 99, "sessions": 1}])
        self.assertEqual(health.assess(value, reading(), NOW).cycles, 2)

    def testMonthlySeries(self):
        value = summary(monthly=[{"month": "2026-10", "drain": 33.0, "charge": [[2, 0.4], [None, 0.5]], "drained": 0, "sessions": 1}])
        result = health.assess(value, reading(), NOW)
        self.assertEqual(result.hoursPerCharge, [("2026-10", 55.0)])
        self.assertEqual(result.chargingByPower, {"wireless charger": [("2026-10", 4.0)], "charger": [("2026-10", 5.0)]})

    def testLastChargedAndChargingSpeedFollowTheLastChargesPower(self):
        ended = time.mktime((2026, 10, 9, 12, 0, 0, 0, 0, -1))
        lastCharge = {"kind": 1, "startAt": ended - 3600, "endAt": ended, "startPercent": 20, "endPercent": 100, "power": 2, "readingCount": 40, "slowCount": 0, "errorCount": 0, "inferred": 0}
        value = summary(lastCharge=lastCharge, monthly=[{"month": "2026-10", "drain": None, "charge": [[1, 0.9], [2, 0.42]], "drained": 0, "sessions": 1}])
        result = health.assess(value, reading(), NOW)
        self.assertEqual(result.lastCharged, "Oct 9, wireless charger")
        self.assertEqual(result.chargingSpeed, "4 min per 10%")

    def testLastChargedIsInEnglishWhateverTheLocale(self):
        ended = time.mktime((2026, 3, 5, 12, 0, 0, 0, 0, -1))
        lastCharge = {"kind": 1, "startAt": ended - 3600, "endAt": ended, "startPercent": 20, "endPercent": 100, "power": 2, "readingCount": 40, "slowCount": 0, "errorCount": 0, "inferred": 0}
        real = time.strftime
        asked = []

        def strftime(fmt, *args):
            if (sys._getframe(1).f_globals["__name__"] == "health"):
                asked.append(fmt)
            return real(fmt.replace("%b", "?").replace("%a", "?"), *args)
        with mock.patch.object(time, "strftime", strftime):
            result = health.assess(summary(lastCharge=lastCharge), reading(), NOW)
        self.assertEqual(result.lastCharged, "Mar 5, wireless charger")
        self.assertEqual([fmt for fmt in asked if "%b" in fmt or "%a" in fmt], [])

    def testPowerLabels(self):
        self.assertEqual([health.powerLabel(power) for power in (2, 1, 0, None)], ["wireless charger", "charger 1", "charger", "charger"])


if (__name__ == "__main__"):
    unittest.main()
