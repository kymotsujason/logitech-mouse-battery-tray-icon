import os
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import hidpp
import lowBattery
import mice
import upower
from serviceState import ServiceEntry, ServiceState

app = QApplication.instance() or QApplication([])
NAME = "PRO X3 SUPERSTRIKE"
RECEIVER = "/dev/hidraw18"
OTHER_NODE = "/dev/hidraw10"
UPOWER_PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"


def entry(key="02bc524c", percent=81, asleep=False, lastRead=1000.0, wokeAt=900.0, source=RECEIVER, name=NAME, pathKey=False, reportsItself=False, historyRevision=0):
    reading = None if percent is None else hidpp.BatteryReading(percent, 0)
    return ServiceEntry(key, pathKey, name, source, reading, asleep, lastRead, wokeAt, reportsItself, False, historyRevision)


def state(*entries, searching=False, nodes=(RECEIVER,), denied=(), busy=False):
    return ServiceState(busy, searching, tuple(nodes), tuple(denied), tuple(entries))


class MouseListTests(unittest.TestCase):
    def setUp(self):
        self.mice = mice.MouseList()
        self.warnings = []
        self.changes = []
        self.mice.changed.connect(lambda: self.changes.append(1))

    def tearDown(self):
        self.mice.stop()
        sip.delete(self.mice)

    def keys(self):
        return [mouse.key for mouse in self.mice.mice]

    def mouse(self):
        return self.mice.mice[0] if self.mice.mice else None

    def byKey(self, key):
        return next(mouse for mouse in self.mice.mice if mouse.key == key)

    def sentWarnings(self):
        def onLowBattery(key, name, percent):
            self.warnings.append(percent)
            # the tray marks a warning sent once its notification goes out
            self.mice.markWarningSent(key, percent)

        self.mice.lowBattery.connect(onLowBattery)
        return self.warnings

    def addUPower(self, percent, key="02bc524c", path=UPOWER_PATH):
        self.mice.applyUPower(upower.UPowerMouse(path, key, "PRO X3", hidpp.BatteryReading(percent, 0)))

    def testAMouseWithNoUPowerHalfCopiesTheEntry(self):
        self.mice.applyService(state(entry(asleep=True, lastRead=1000.0, wokeAt=900.0)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.name, mouse.nodePath, mouse.reading, mouse.asleep, mouse.lastRead, mouse.wokeAt), (NAME, RECEIVER, hidpp.BatteryReading(81, 0), True, 1000.0, 900.0))

    def testTheShownMouseFollowsTheEntriesWokeAt(self):
        self.mice.applyService(state(entry("aaaaaaaa", wokeAt=100.0, lastRead=300.0), entry("bbbbbbbb", wokeAt=200.0, lastRead=250.0, source=OTHER_NODE), nodes=(RECEIVER, OTHER_NODE)))
        self.assertEqual(self.mice.shownMouse().key, "bbbbbbbb")

    def testTheShownMouseIsTheOneThatWokeLast(self):
        awake = entry("bbbbbbbb", wokeAt=200.0, lastRead=250.0, source=OTHER_NODE)
        self.mice.applyService(state(entry("aaaaaaaa", asleep=True, wokeAt=300.0, lastRead=300.0), awake, nodes=(RECEIVER, OTHER_NODE)))
        self.assertEqual(self.mice.shownMouse().key, "bbbbbbbb")
        self.mice.applyService(state(entry("aaaaaaaa", wokeAt=400.0, lastRead=400.0), awake, nodes=(RECEIVER, OTHER_NODE)))
        self.assertEqual(self.mice.shownMouse().key, "aaaaaaaa")

    def testMiceWithTheSameNameGetNumbers(self):
        self.mice.applyService(state(entry("aaaaaaaa"), entry("bbbbbbbb", source=OTHER_NODE), nodes=(RECEIVER, OTHER_NODE)))
        self.assertEqual(sorted(self.mice.displayNames().values()), [NAME, NAME + " 2"])

    def testSameNamedMiceKeepTheirNumbersWhenOneComesBack(self):
        both = state(entry("aaaaaaaa"), entry("bbbbbbbb", source=OTHER_NODE), nodes=(RECEIVER, OTHER_NODE))
        self.mice.applyService(both)
        # the first mouse leaves and comes back, which puts it at the end of the list
        self.mice.applyService(state(entry("bbbbbbbb", source=OTHER_NODE), nodes=(OTHER_NODE,)))
        self.mice.applyService(both)
        self.assertEqual(self.mice.displayNames(), {"aaaaaaaa": NAME, "bbbbbbbb": NAME + " 2"})

    def testALowReadingWarnsOnce(self):
        warnings = self.sentWarnings()
        self.mice.applyService(state(entry(percent=9, lastRead=1000.0)))
        self.mice.applyService(state(entry(percent=9, lastRead=1000.0)))
        self.mice.applyService(state(entry(percent=9, lastRead=1060.0)))
        self.assertEqual(warnings, [9])

    def testAnAsleepEntryWithALowCachedReadingDoesntWarn(self):
        warnings = self.sentWarnings()
        self.mice.applyService(state(entry(percent=4, asleep=True)))
        self.assertEqual(warnings, [])

    def testAMouseThatLosesItsUPowerHalfTakesTheServicesSleep(self):
        self.mice.applyService(state(entry(percent=40, asleep=True, lastRead=1000.0)))
        self.addUPower(50)
        self.assertEqual((self.mouse().reading.percent, self.mouse().asleep), (50, False))
        self.mice.removeUPower(UPOWER_PATH)
        self.assertEqual((self.keys(), self.mouse().reading.percent, self.mouse().asleep, self.mouse().upowerPath, self.mouse().stale), (["02bc524c"], 50, True, None, False))

    def testAMouseThatLosesItsUPowerHalfWithNoServiceTurnsAsleep(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.applyService(None)
        self.mice.removeUPower(UPOWER_PATH)
        self.assertEqual((self.mouse().reading.percent, self.mouse().asleep, self.mouse().stale), (50, True, True))

    def testAStaleMouseThatUPowerReadsIsCurrentAgain(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.mice.applyService(None)
        self.assertTrue(self.mouse().stale)
        self.addUPower(50)
        self.assertEqual((self.mouse().reading.percent, self.mouse().asleep, self.mouse().stale), (50, False, False))

    def testAnOlderEntryAfterUPowerLeavesGivesOnlyItsSleep(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.removeUPower(UPOWER_PATH)
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0, asleep=True)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.asleep), (50, True))

    def testAnAsleepEntryBeforeUPowerLeavesEndsTheSame(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0, asleep=True)))
        self.mice.removeUPower(UPOWER_PATH)
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.asleep), (50, True))

    def testNoneAfterUPowerLeavesDoesntWarnAgain(self):
        warnings = self.sentWarnings()
        self.mice.applyService(state(entry(percent=60, lastRead=1000.0)))
        self.addUPower(9)
        self.mice.removeUPower(UPOWER_PATH)
        self.mice.applyService(None)
        self.mice.applyService(state(entry(percent=60, lastRead=1000.0)))
        self.assertEqual(self.byKey("02bc524c").reading.percent, 9)
        self.mice.applyService(state(entry(percent=9, lastRead=time.time() + 10)))
        self.assertEqual(warnings, [9])

    def testALaterEntryAfterUPowerLeavesIsCopied(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.removeUPower(UPOWER_PATH)
        self.mice.applyService(state(entry(percent=30, lastRead=time.time() + 10)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.readFromUPower), (30, False))

    def testAMouseThatLosesItsUPowerHalfToAnAwakeEntryStaysAwake(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.removeUPower(UPOWER_PATH)
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.asleep, mouse.reading.percent), (False, 50))

    def testAMouseThatLosesItsUPowerHalfWhileSearchingTurnsAsleep(self):
        self.mice.applyService(state(entry(percent=40, lastRead=1000.0)))
        self.addUPower(50)
        self.mice.applyService(state(searching=True, nodes=(RECEIVER,)))
        self.mice.removeUPower(UPOWER_PATH)
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.asleep, mouse.reading.percent), (True, 50))

    def testAMergedMouseKeepsANewerUPowerReading(self):
        self.addUPower(50)
        self.mice.applyService(state(entry(percent=40, lastRead=time.time() - 100)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((self.keys(), mouse.reading.percent, mouse.nodePath, mouse.upowerPath), (["02bc524c"], 50, RECEIVER, UPOWER_PATH))

    def testAMergedMouseTakesALaterEntry(self):
        self.addUPower(50)
        later = time.time() + 10
        self.mice.applyService(state(entry(percent=40, lastRead=later, wokeAt=later)))
        self.assertEqual((self.byKey("02bc524c").reading.percent, self.byKey("02bc524c").lastRead), (40, later))

    def testAMergedMouseCopiesNothingFromANullLastRead(self):
        self.addUPower(50)
        self.mice.applyService(state(entry(percent=None, asleep=True, lastRead=None, wokeAt=None)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.asleep, mouse.nodePath), (50, False, RECEIVER))

    def testAnAsleepOnlyChangeLeavesAMergedMouseAlone(self):
        self.addUPower(50)
        older = time.time() - 100
        self.mice.applyService(state(entry(percent=40, lastRead=older)))
        self.mice.applyService(state(entry(percent=40, lastRead=older, asleep=True)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.asleep), (50, False))

    def testAMergedMouseKeepsANewerUPowerReadingAfterNone(self):
        self.addUPower(50)
        old = state(entry(percent=40, lastRead=time.time() - 100))
        self.mice.applyService(old)
        self.mice.applyService(None)
        self.mice.applyService(old)
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading.percent, mouse.asleep), (50, False))

    def testANewKeyWithNoReadingTakesTheEntrysAsleep(self):
        self.mice.applyService(state(entry(percent=None, asleep=True, lastRead=None, wokeAt=None)))
        mouse = self.byKey("02bc524c")
        self.assertEqual((mouse.reading, mouse.asleep), (None, True))

    def testAKeyThatGoesAndComesBackWithTheSameEntryIsAppliedAgain(self):
        self.mice.applyService(state(entry()))
        self.mice.applyService(state())
        self.assertEqual(self.keys(), [])
        self.mice.applyService(state(entry()))
        self.assertEqual(self.keys(), ["02bc524c"])

    def testASearchingStateKeepsAKeyWhoseNodeIsStillOpen(self):
        self.mice.applyService(state(entry()))
        self.mice.applyService(state(searching=True, nodes=(RECEIVER,)))
        self.assertEqual(self.keys(), ["02bc524c"])
        self.mice.applyService(state(searching=True, nodes=(OTHER_NODE,)))
        self.assertEqual(self.keys(), [])

    def testNoneThenTheSameStateWakesTheMiceAndDoesntWarnTwice(self):
        warnings = self.sentWarnings()
        low = state(entry(percent=9))
        self.mice.applyService(low)
        self.mice.applyService(None)
        self.assertTrue(self.byKey("02bc524c").asleep)
        self.mice.applyService(low)
        self.assertEqual((self.byKey("02bc524c").asleep, warnings), (False, [9]))

    def testNoneLeavesAMouseUPowerAlsoReportsAwake(self):
        self.addUPower(50)
        self.mice.applyService(state(entry(percent=40, lastRead=time.time() - 100)))
        self.mice.applyService(None)
        self.assertFalse(self.byKey("02bc524c").asleep)

    def testAKeyTheFirstStateAfterNoneDoesntListLosesItsHalf(self):
        self.mice.applyService(state(entry()))
        self.mice.applyService(None)
        self.mice.applyService(state())
        self.assertEqual(self.keys(), [])

    def testEveryStateEmitsChanged(self):
        self.mice.applyService(state(searching=True))
        self.mice.applyService(state(searching=False))
        self.mice.applyService(None)
        self.assertEqual(len(self.changes), 3)

    def testStatus(self):
        self.assertEqual(self.mice.status(), mice.STATUS_SEARCHING)
        self.mice.applyService(None)
        self.assertEqual(self.mice.status(), mice.STATUS_NO_SERVICE)
        self.assertEqual(mice.STATUS_TEXT[mice.STATUS_NO_SERVICE], "Can't reach the battery service.")
        self.mice.applyService(state(searching=True, nodes=(RECEIVER,)))
        self.assertEqual(self.mice.status(), mice.STATUS_SEARCHING)
        self.mice.applyService(state(nodes=(RECEIVER,)))
        self.assertEqual(self.mice.status(), mice.STATUS_WAITING)
        self.mice.applyService(state(nodes=(), denied=(RECEIVER,)))
        self.assertEqual(self.mice.status(), mice.STATUS_DENIED)
        self.mice.applyService(state(nodes=()))
        self.assertEqual(self.mice.status(), mice.STATUS_NONE)
        self.addUPower(55, key="12ab34cd", path="/p")
        self.assertEqual(self.mice.status(), mice.STATUS_MICE)
        self.mice.applyService(None)
        self.assertEqual(self.mice.status(), mice.STATUS_MICE)

    def testReadAllAsksTheServiceToRead(self):
        requests = []
        self.mice.readRequested.connect(lambda *flags: requests.append(flags))
        self.mice.readAll()
        self.mice.readTimer.start(10)
        QTest.qWait(50)
        self.mice.readTimer.stop()
        # the flag lets the read start the service, which readAll's callers always get and a timed read gets only once an hour
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(requests, [(True,)] + [(False,)] * (len(requests) - 1))

    def testATimedReadMayStartTheServiceOnceAnHour(self):
        clock = [5000.0]
        starting = []
        with mock.patch.object(time, "monotonic", lambda: clock[0]):
            mouseList = mice.MouseList()
            self.addCleanup(sip.delete, mouseList)
            mouseList.readRequested.connect(lambda activate: activate and starting.append(round((clock[0] - 5000.0) / 60)))
            for minute in range(5, 185, 5):
                clock[0] = 5000.0 + minute * 60
                if (minute == 100):
                    mouseList.readAll()
                mouseList.readOnTimer()
        # an hour is long past the unit's 60 s start limit, and a person's read starts the hour over since it already tried
        self.assertEqual(starting, [60, 100, 160])

    def warningsAcrossTheNodeGoing(self, key, pathKey):
        warnings = self.sentWarnings()
        self.mice.applyService(state(entry(key, percent=9, pathKey=pathKey)))
        self.mice.applyService(state(nodes=()))
        self.mice.applyService(state(entry(key, percent=9, lastRead=2000.0, pathKey=pathKey)))
        return warnings

    def testAMouseKeyedByNodeAndSlotIsWarnedAgainAfterTheNodeGoes(self):
        self.assertEqual(self.warningsAcrossTheNodeGoing(RECEIVER + "#1", True), [9, 9])

    def testAMouseWithAUnitIdIsntWarnedAgainAfterTheNodeGoes(self):
        self.assertEqual(self.warningsAcrossTheNodeGoing("02bc524c", False), [9])

    def testFreshUnitIdsOnOneSlotWarnAFewTimesAndLeaveNothingBehind(self):
        warnings = self.sentWarnings()
        for i in range(20):
            self.mice.applyService(state(entry("%08x" % i, percent=9)))
        self.assertEqual(len(warnings), lowBattery.NOTICE_LIMIT)
        # only the mouse still in the list can have anything armed
        self.assertLessEqual(set(self.mice.warner.sent), {"%08x" % 19})

    def testAUPowerMouseMergesWithTheSameHidppMouse(self):
        warnings = self.sentWarnings()
        self.mice.applyService(state(entry(percent=81, lastRead=1000.0)))
        self.addUPower(9)
        self.assertEqual((self.keys(), self.byKey("02bc524c").upowerPath, warnings), (["02bc524c"], UPOWER_PATH, [9]))
        self.mice.removeUPower(UPOWER_PATH)
        self.assertEqual((self.keys(), self.byKey("02bc524c").upowerPath), (["02bc524c"], None))
        # the HID++ half reads 9% as well, and the warning the mouse already got still counts
        self.mice.applyService(state(entry(percent=9, lastRead=time.time() + 10)))
        self.assertEqual(warnings, [9])

    def upowerWarningsAcrossARemoval(self, path, key):
        warnings = self.sentWarnings()
        lowMouse = upower.UPowerMouse(path, key, "MX Master 3", hidpp.BatteryReading(9, 0), pathKey=(key == path))
        self.mice.applyUPower(lowMouse)
        self.mice.removeUPower(path)
        self.mice.applyUPower(lowMouse)
        return warnings

    def testAUPowerMouseKeyedByItsPathIsWarnedAgainAfterARemoval(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.assertEqual(self.upowerWarningsAcrossARemoval(path, path), [9, 9])

    def testAUPowerMouseWithASerialIsntWarnedAgainAfterARemoval(self):
        self.assertEqual(self.upowerWarningsAcrossARemoval("/org/freedesktop/UPower/devices/mouse_hidpp_battery_1", "12ab34cd"), [9])

    def testAUPowerOnlyMouseComesAndGoes(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual(self.keys(), ["12ab34cd"])
        self.assertFalse(self.mouse().asleep)
        self.mice.removeUPower(path)
        self.assertEqual(self.keys(), [])

    def testAUPowerReadingTakesTheTimeUPowerReadItAt(self):
        self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0), updatedAt=1000.0))
        self.assertEqual(self.mouse().lastRead, 1000.0)

    def testAUPowerReadingFromTheFutureTakesTheTimeItCameIn(self):
        with mock.patch.object(mice.time, "time", return_value=2000.0):
            self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0), updatedAt=9000.0))
        self.assertEqual(self.mouse().lastRead, 2000.0)

    def testAnOlderUPowerReadingDoesntReplaceANewerServiceReading(self):
        self.mice.applyService(state(entry(key="12ab34cd", percent=40, lastRead=1900.0)))
        self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(80, 0), updatedAt=1000.0))
        self.assertEqual((self.mouse().reading.percent, self.mouse().lastRead, self.mouse().upowerPath), (40, 1900.0, UPOWER_PATH))

    def testAnOlderUPowerReadingAfterTheServiceIsLostMarksTheMouseCurrent(self):
        self.mice.applyService(state(entry(key="12ab34cd", percent=40, lastRead=1900.0)))
        self.mice.applyService(None)
        self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(80, 0), updatedAt=1000.0))
        self.assertEqual((self.mouse().reading.percent, self.mouse().stale, self.mouse().asleep), (40, False, False))

    def testAUPowerPathThatGainsASerialStaysOneMouse(self):
        self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, UPOWER_PATH, "MX Master 3", hidpp.BatteryReading(55, 0), pathKey=True))
        self.mice.applyUPower(upower.UPowerMouse(UPOWER_PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(54, 0)))
        self.assertEqual(self.keys(), ["12ab34cd"])

    def testAChangedUPowerReadingCountsAsWaking(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        firstWake = self.mouse().wokeAt
        time.sleep(0.02)
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual(self.mouse().wokeAt, firstWake)
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(54, 0)))
        self.assertGreater(self.mouse().wokeAt, firstWake)


class TimedReadTests(unittest.TestCase):
    def setUp(self):
        self.mice = mice.MouseList()
        self.reads = []
        self.mice.readRequested.connect(lambda activate: self.reads.append((mice.READ_ALL, activate)))
        self.mice.readSilentRequested.connect(lambda activate: self.reads.append((mice.READ_SILENT, activate)))
        self.mice.start()

    def tearDown(self):
        self.mice.stop()
        sip.delete(self.mice)

    def timer(self):
        return (self.mice.readTimer.isActive(), self.mice.readTimer.interval() if self.mice.readTimer.isActive() else None, self.mice.timedMethod)

    def testBeforeTheServiceAnswersTheTimerReadsEverythingEveryFiveMinutes(self):
        self.assertEqual(self.timer(), (True, mice.READ_INTERVAL_MS, mice.READ_ALL))

    def testAMouseThatDoesntReportOnItsOwnGetsASilentRead(self):
        self.mice.applyService(state(entry(reportsItself=False)))
        self.assertEqual(self.timer(), (True, mice.READ_INTERVAL_MS, mice.READ_SILENT))

    def testEveryMouseReportingOnItsOwnStopsTheTimer(self):
        self.mice.applyService(state(entry(reportsItself=True)))
        self.assertEqual(self.timer(), (False, None, None))

    def testALostServiceGoesBackToReadingEverything(self):
        self.mice.applyService(state(entry(reportsItself=True)))
        self.mice.applyService(None)
        self.assertEqual(self.timer(), (True, mice.READ_INTERVAL_MS, mice.READ_ALL))

    def testAServiceWithNoMiceStillReadsEverything(self):
        self.mice.applyService(state())
        self.assertEqual(self.timer(), (True, mice.READ_INTERVAL_MS, mice.READ_ALL))

    def testTheSettingReadsEverythingAtItsInterval(self):
        self.mice.applyService(state(entry(reportsItself=True)))
        self.mice.setSleepCheck(30)
        self.assertEqual(self.timer(), (True, 30000, mice.READ_ALL))
        self.mice.setSleepCheck(0)
        self.assertEqual(self.timer(), (False, None, None))

    def testAStreamOfStatesDoesntPushTheReadOut(self):
        self.mice.applyService(state(entry(reportsItself=False)))
        QTest.qWait(50)
        before = self.mice.readTimer.remainingTime()
        self.mice.applyService(state(entry(reportsItself=False, percent=80)))
        self.assertLessEqual(self.mice.readTimer.remainingTime(), before)

    def testATickSendsTheTimedMethod(self):
        self.mice.applyService(state(entry(reportsItself=False)))
        self.mice.readOnTimer()
        self.assertEqual(self.reads[-1][0], mice.READ_SILENT)
        self.mice.setSleepCheck(60)
        self.mice.readOnTimer()
        self.assertEqual(self.reads[-1][0], mice.READ_ALL)

    def testTheEntrysFlagsReachTheMouse(self):
        self.mice.applyService(state(entry(reportsItself=True, historyRevision=4)))
        self.assertEqual((self.mice.mice[0].reportsItself, self.mice.mice[0].historyRevision), (True, 4))


if (__name__ == "__main__"):
    unittest.main()
