import calendar
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

    def testLearningWithoutThreeBucketsShowsAClock(self):
        value = summary(drain={"buckets": readyBuckets((80,), 30.0, (30.0, 30.0, 30.0)), "largeSteps": False})
        self.assertEqual(self.lines(value), ([(health.LEARNING, "Still learning. The wear check needs charges that run the battery down to about 55%.")], health.SYMBOL_CLOCK))

    def testLearningNamesTheDepthWhenOnlyOneOrTwoRangesHaveSessions(self):
        for sessions in ((9, 0, 0, 0, 0, 0), (30, 12, 0, 0, 0, 0), (0, 0, 0, 0, 0, 3)):
            buckets = [bucket(top, sessions=count) for top, count in zip((80, 70, 60, 50, 40, 30), sessions)]
            value = summary(drain={"buckets": buckets, "largeSteps": False})
            self.assertEqual(self.lines(value)[0], [(health.LEARNING, "Still learning. The wear check needs charges that run the battery down to about 55%.")], sessions)

    def testLearningWithAUseFigureButNoSessionsSaysTheWearCheckStarts(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}]
        self.assertEqual(self.lines(summary(monthly=monthly))[0][0], (health.LEARNING, "Still learning. The wear check starts after a few more charges."))

    def testLearningCountsDownTheChargesLeftInTheThirdBestRange(self):
        def learning(sessions):
            buckets = [bucket(top, sessions=count) for top, count in zip((80, 70, 60, 50, 40, 30), sessions)]
            value = summary(drain={"buckets": buckets, "largeSteps": False})
            return self.lines(value)[0]
        self.assertEqual(learning((0, 0, 0, 0, 0, 0)), [(health.LEARNING, "Still learning. The first figures come after a few charges.")])
        self.assertEqual(learning((30, 20, 20, 20, 5, 0)), [(health.LEARNING, "Still learning. The wear check needs about 22 more charges.")])
        self.assertEqual(learning((9, 5, 5, 0, 0, 0)), [(health.LEARNING, "Still learning. The wear check needs about 37 more charges.")])
        self.assertEqual(learning((41, 41, 41, 0, 0, 0)), [(health.LEARNING, "Still learning. The wear check needs about 1 more charge.")])
        self.assertEqual(learning((80, 60, 50, 0, 0, 0)), [(health.LEARNING, "Still learning. The wear check needs about 1 more charge.")])

    def testNoTextSaysBatteryRanges(self):
        for value in (summary(), summary(drain={"buckets": readyBuckets((80,), 30.0, (30.0, 30.0, 30.0)), "largeSteps": False})):
            for kind, text in self.lines(value)[0]:
                self.assertNotIn("battery ranges", text)

    def testTheUseLineComesFromTheNewestMonthWithADrainFigure(self):
        monthly = [{"month": "2026-09", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}, {"month": "2026-10", "drain": None, "charge": [], "drained": 0, "sessions": 1}]
        lines, symbol = self.lines(summary(monthly=monthly))
        self.assertEqual(lines, [(health.LEARNING, "Still learning. The wear check starts after a few more charges."), (health.INFO, "A full charge gives about 42 hours of use.")])
        self.assertEqual(symbol, health.SYMBOL_CLOCK)
        short = [{"month": "2026-10", "drain": 0.25, "charge": [], "drained": 0, "sessions": 1}]
        self.assertEqual(self.lines(summary(monthly=short))[0][1], (health.INFO, "A full charge gives about 25 min of use."))

    def testTheUseLineShowsNextToTheGoodLine(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}]
        value = summary(monthly=monthly, drain={"buckets": readyBuckets((80, 70, 60), 30.0, (29.0, 30.0, 31.0)), "largeSteps": False})
        lines, symbol = self.lines(value)
        self.assertEqual(lines, [(health.GOOD, health.GOOD_TEXT), (health.INFO, "A full charge gives about 42 hours of use.")])
        self.assertEqual(symbol, health.SYMBOL_CHECK)

    def testTheChargeTimeLineNamesTheLastCharger(self):
        good = {"buckets": readyBuckets((80, 70, 60), 30.0, (29.0, 30.0, 31.0)), "largeSteps": False}
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [[1, 0.9], [2, 0.42]], "drained": 0, "sessions": 1}]
        for power, text in ((2, "Charging from 20% to 80% takes about 25 min on the wireless charger."), (1, "Charging from 20% to 80% takes about 54 min on the cable.")):
            lastCharge = {"kind": 1, "startAt": NOW - 3600, "endAt": NOW, "startPercent": 20, "endPercent": 100, "power": power, "readingCount": 40, "slowCount": 0, "errorCount": 0, "inferred": 0}
            lines = self.lines(summary(monthly=monthly, lastCharge=lastCharge, drain=good))[0]
            self.assertEqual([kind for kind, line in lines], [health.GOOD, health.INFO, health.INFO])
            self.assertEqual(lines[2][1], text)

    def testTheChargeTimeReadsInMinutesThenHoursAndFiveMinutes(self):
        self.assertEqual([health.chargeTimeText(minutes) for minutes in (0.2, 25.2, 54, 59.4, 59.6, 60, 62, 96, 100, 118, 120, 121, 150)], ["1 min", "25 min", "54 min", "59 min", "1 hour", "1 hour", "1 hour", "1 hour 35 min", "1 hour 40 min", "2 hours", "2 hours", "2 hours", "2 hours 30 min"])

    def testTheChargeTimeLineNeedsALastChargeWithAFigure(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [[1, 0.9]], "drained": 0, "sessions": 1}]
        self.assertEqual(len(self.lines(summary(monthly=monthly))[0]), 2)
        lastCharge = {"kind": 1, "startAt": NOW - 3600, "endAt": NOW, "startPercent": 20, "endPercent": 100, "power": 2, "readingCount": 40, "slowCount": 0, "errorCount": 0, "inferred": 0}
        self.assertEqual(len(self.lines(summary(monthly=monthly, lastCharge=lastCharge))[0]), 2)

    def testARoughLevelMouseGetsNoUseLine(self):
        rough = hidpp.BatteryReading(None, 0, level="good")
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}]
        self.assertEqual(self.lines(summary(wakeSupport=False, monthly=monthly), rough)[0], [(health.LEARNING, health.ROUGH_TEXT)])

    def testNoUseLineAfterAMessageSayingDrainCantBeMeasured(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}]
        large = summary(monthly=monthly, drain={"buckets": readyBuckets((), 0, ()), "largeSteps": True})
        self.assertEqual(self.lines(large)[0], [(health.LEARNING, health.LARGE_STEPS_TEXT)])
        unseen = summary(monthly=monthly, wakesVisible=False)
        self.assertEqual(self.lines(unseen)[0], [(health.LEARNING, health.WAKES_UNSEEN_TEXT)])
        noWakes = summary(monthly=monthly, wakeSupport=False)
        self.assertEqual(self.lines(noWakes)[0], [(health.LEARNING, health.NO_WAKES_TEXT)])
        voltage = hidpp.BatteryReading(None, 0, millivolts=3850)
        self.assertEqual(self.lines(summary(wakeSupport=False, monthly=monthly), voltage)[0], [(health.LEARNING, health.VOLTAGE_TEXT)])

    def testTheUseLineSkipsAMonthThatJustStarted(self):
        monthly = [{"month": "2026-12", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}, {"month": "2027-01", "drain": 0.5, "charge": [], "drained": 0, "sessions": 1}]
        value = summary(monthly=monthly)
        lastDay = calendar.timegm((2027, 1, 7, 23, 59, 59, 0, 0, 0))
        nextDay = calendar.timegm((2027, 1, 8, 0, 0, 0, 0, 0, 0))
        useText = lambda moment: health.assess(value, reading(), moment).lines[-1].text
        self.assertEqual(useText(lastDay), "A full charge gives about 42 hours of use.")
        self.assertEqual(useText(nextDay), "A full charge gives about 50 min of use.")
        # without an earlier month there's nothing to fall back on
        self.assertEqual(health.assess(summary(monthly=monthly[1:]), reading(), lastDay).lines[-1].text, "A full charge gives about 50 min of use.")

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

    def testAUPowerMouseNotYetSeenSleepingIsToldWhenDrainStarts(self):
        self.assertEqual(health.UPOWER_NO_SLEEP_TEXT, "Tracking has started. Drain is measured once this mouse has been seen falling asleep and waking up, and charging speed already is.")
        upowerValue = summary(wakeSupport=False, source=history.SOURCE_UPOWER)
        self.assertEqual(self.lines(upowerValue)[0], [(health.LEARNING, health.UPOWER_NO_SLEEP_TEXT)])
        hidppValue = summary(wakeSupport=False, source=history.SOURCE_HIDPP)
        self.assertEqual(self.lines(hidppValue)[0], [(health.LEARNING, health.NO_WAKES_TEXT)])

    def testTheUPowerLineHidesTheUseLineLikeTheOtherCapabilityLines(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [], "drained": 0, "sessions": 1}]
        value = summary(wakeSupport=False, source=history.SOURCE_UPOWER, monthly=monthly)
        self.assertEqual(self.lines(value)[0], [(health.LEARNING, health.UPOWER_NO_SLEEP_TEXT)])
        rough = hidpp.BatteryReading(None, 0, level="good")
        self.assertEqual(self.lines(summary(wakeSupport=False, source=history.SOURCE_UPOWER), rough)[0], [(health.LEARNING, health.ROUGH_TEXT)])

    def testAMouseWhoseWakesArentVisibleYetIsntStillLearning(self):
        value = summary(wakesVisible=False, drain={"buckets": readyBuckets((80,), 30.0, (30.0, 30.0, 30.0)), "largeSteps": False})
        self.assertEqual(self.lines(value), ([(health.LEARNING, health.WAKES_UNSEEN_TEXT)], health.SYMBOL_CLOCK))
        # a service from before the field still gets its learning count
        old = summary()
        del old["wakesVisible"]
        self.assertEqual(self.lines(old)[0][0][1], "Still learning. The first figures come after a few charges.")

    def testAFreshMouseWhoseLookupHasntAnsweredIsntSaidToLackWakes(self):
        value = summary(wakeSupport=False, wakesVisible=False)
        self.assertEqual(self.lines(value), ([(health.LEARNING, health.WAKES_UNSEEN_TEXT)], health.SYMBOL_CLOCK))

    def testALargeStepsMouseKeepsItsLineWhileWakesAreUnseen(self):
        value = summary(wakesVisible=False, drain={"buckets": readyBuckets((), 0, ()), "largeSteps": True})
        self.assertEqual(self.lines(value), ([(health.LEARNING, health.LARGE_STEPS_TEXT)], health.SYMBOL_CLOCK))

    def testNoMoreThanThreeLines(self):
        value = summary(lastError=NOW, drain={"buckets": readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 24.0)), "largeSteps": False}, charge=[{"power": 2, "buckets": readyBuckets((20, 30, 40), 0.5, (0.6, 0.6, 0.6))}])
        self.assertEqual([kind for kind, text in self.lines(value)[0]], [health.ALERT, health.DRAIN, health.CHARGE_LINE_KIND])


    def testTheChargeTimeLineIsTheFirstToGoWhenLinesRunOut(self):
        monthly = [{"month": "2026-10", "drain": 25.0, "charge": [[2, 0.42]], "drained": 0, "sessions": 1}]
        lastCharge = {"kind": 1, "startAt": NOW - 3600, "endAt": NOW, "startPercent": 20, "endPercent": 100, "power": 2, "readingCount": 40, "slowCount": 0, "errorCount": 0, "inferred": 0}
        value = summary(lastError=NOW, monthly=monthly, lastCharge=lastCharge, drain={"buckets": readyBuckets((80, 70, 60), 30.0, (24.0, 24.0, 24.0)), "largeSteps": False})
        lines = self.lines(value)[0]
        self.assertEqual([kind for kind, text in lines], [health.ALERT, health.DRAIN, health.INFO])
        self.assertEqual(lines[0][1], health.CHARGING_ERROR_TEXT)
        self.assertTrue(lines[1][1].startswith("A charge lasts about 20% less"))
        self.assertEqual(lines[2][1], "A full charge gives about 42 hours of use.")


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
        self.assertEqual([health.powerLabel(power) for power in (2, 1, 0, None)], ["wireless charger", "cable", "charger", "charger"])


if (__name__ == "__main__"):
    unittest.main()
