import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

import hidpp
import history

app = QApplication.instance() or QApplication([])
KEY = "02bc524c"
START = 1800000000


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


def battery(percent, charging=0, power=0, level=None, millivolts=None):
    return hidpp.BatteryReading(percent, charging, millivolts, level, power)


def corruption():
    error = sqlite3.DatabaseError("database disk image is malformed")
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    return error


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.errors = io.StringIO()
        self.patch = mock.patch.object(sys, "stderr", self.errors)
        self.patch.start()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)

    def tearDown(self):
        self.recorder.stop()
        self.patch.stop()
        shutil.rmtree(self.folder)

    def at(self, seconds):
        # moves the clock and lets every reading older than the wake set wait through
        self.clock.now = START + seconds
        self.recorder.tick()

    def event(self, seconds, percent, charging=0, power=0, key=KEY, origin=history.ORIGIN_EVENT, **kwargs):
        self.clock.now = START + seconds
        self.recorder.addReading(key, battery(percent, charging, power, **kwargs), origin, name="PRO X3 SUPERSTRIKE", at=START + seconds)
        self.at(seconds + history.WAKE_SET_SECONDS)

    def marker(self, seconds, origin, key=KEY):
        self.clock.now = START + seconds
        self.recorder.addMarker(key, origin, at=START + seconds)

    def db(self):
        return sqlite3.connect(os.path.join(self.folder, history.FILE_NAME))

    def failOnce(self, error):
        real = self.recorder.store.transaction
        calls = []

        def failingOnce():
            calls.append(1)
            if (len(calls) == 1):
                raise error
            return real()
        return mock.patch.object(self.recorder.store, "transaction", failingOnce)

    def failQuery(self, error, match=""):
        # only the first statement that matches fails, so a store that didn't stop would take the next write
        self.assertTrue(self.recorder.available())
        real = self.recorder.store.db
        failed = []

        class FailingDb:
            def execute(self, sql, *args):
                if (match in sql and not failed):
                    failed.append(sql)
                    raise error
                return real.execute(sql, *args)

            def close(self):
                real.close()
        self.recorder.store.db = FailingDb()
        return real

    def assertStoppedForCorruption(self, seconds):
        self.assertFalse(self.recorder.available())
        self.assertTrue(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        saved = self.rows()
        self.event(seconds, 40)
        self.assertEqual(self.rows(), saved)

    def rows(self):
        with self.db() as db:
            return db.execute("select at - ?, origin, percent, charging, power from readings order by at, seq", (START,)).fetchall()

    def sessions(self):
        with self.db() as db:
            return db.execute("select kind, startAt - ?, endAt - ?, startPercent, endPercent, power, readingCount, slowCount, errorCount, inferred from sessions order by id", (START, START)).fetchall()

    def steps(self):
        with self.db() as db:
            return db.execute("select kind, at - ?, round(minutesPerPercent, 3), fromPercent, toPercent, power, unbroken from steps order by at, seq", (START,)).fetchall()


class StoreTests(RecorderCase):
    def testANewDatabaseGetsItsSchemaAndVersion(self):
        self.assertTrue(self.recorder.available())
        with self.db() as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 1)
            tables = {name for (name,) in db.execute("select name from sqlite_master where type = 'table'")}
        self.assertEqual(tables, {"mice", "readings", "steps", "sessions"})

    def testTheDatabaseIsInWalMode(self):
        self.assertTrue(self.recorder.available())
        with self.db() as db:
            self.assertEqual(db.execute("pragma journal_mode").fetchone()[0], "wal")

    def testADatabaseWrittenByThisCodeOpensAgain(self):
        self.event(0, 50)
        self.recorder.stop()
        again = history.Recorder(self.folder, self.clock, timers=False)
        self.assertTrue(again.available())
        again.stop()

    def testANewerDatabaseIsLeftAsItIs(self):
        with self.db() as db:
            db.execute("pragma user_version = 2")
        self.assertFalse(self.recorder.available())
        self.event(0, 50)
        with self.db() as db:
            self.assertEqual(db.execute("select count(*) from sqlite_master").fetchone()[0], 0)
        self.assertIn("newer version", self.errors.getvalue())

    def testACorruptFileIsMovedAsideWithItsWriteLogAndANewOneStarts(self):
        path = os.path.join(self.folder, history.FILE_NAME)
        with open(path, "wb") as f:
            f.write(b"x" * 4096)
        for extension in ("-wal", "-shm"):
            with open(path + extension, "wb") as f:
                f.write(b"log")
        self.assertTrue(self.recorder.available())
        broken = path + ".broken-" + str(START)
        self.assertEqual(sorted(name for name in os.listdir(self.folder) if ".broken-" in name), sorted(os.path.basename(broken + extension) for extension in ("", "-wal", "-shm")))
        with open(broken + "-wal", "rb") as f:
            self.assertEqual(f.read(), b"log")
        self.assertEqual(self.errors.getvalue().count("corrupt"), 1)

    def testAFailureThatIsntCorruptionRenamesNothingAndRetriesLater(self):
        real = sqlite3.connect

        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(sqlite3, "connect", failing):
            self.assertFalse(self.recorder.available())
        self.assertEqual([name for name in os.listdir(self.folder) if ".broken-" in name], [])
        # the next try waits out the retry gap, even with the disk back
        self.assertFalse(self.recorder.available())
        self.clock.now += history.OPEN_RETRY_SECONDS
        self.assertTrue(self.recorder.available())
        self.assertIs(sqlite3.connect, real)

    def testAnOpenFailureThatComesBackAfterARecoveryIsPrintedAgain(self):
        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        with mock.patch.object(sqlite3, "connect", failing):
            self.assertFalse(self.recorder.available())
        self.clock.now += history.OPEN_RETRY_SECONDS
        self.assertTrue(self.recorder.available())
        self.recorder.store.closeQuietly()
        with mock.patch.object(sqlite3, "connect", failing):
            self.assertFalse(self.recorder.available())
        self.assertEqual(self.errors.getvalue().splitlines(), ["Couldn't open the history: disk I/O error", "The history is open again", "Couldn't open the history: disk I/O error"])

    def testAReadFailureThatComesBackAfterARecoveryIsPrintedAgain(self):
        def failing(*args):
            raise sqlite3.OperationalError("disk I/O error")
        for attempt in range(2):
            with mock.patch.object(history, "readSummary", failing):
                with self.assertRaises(history.HistoryUnavailable):
                    self.recorder.summary(KEY)
            self.recorder.summary(KEY)
        self.assertEqual(self.errors.getvalue().splitlines(), ["Couldn't read the history: disk I/O error", "The history can be read again"] * 2)

    def testCorruptionDuringARunStopsTheHistoryAndAsksForTheFullCheck(self):
        self.event(0, 50)
        self.recorder.store.markCorrupt(history.CorruptHistory("test"))
        self.assertFalse(self.recorder.available())
        self.assertTrue(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        self.recorder.stop()
        again = history.Recorder(self.folder, self.clock, timers=False)
        with mock.patch.object(again.store, "openOnce", wraps=again.store.openOnce):
            self.assertTrue(again.available())
        self.assertFalse(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        again.stop()

    def testCorruptionFoundByASummaryStopsTheHistory(self):
        self.event(0, 50)
        self.failQuery(corruption(), "from steps")
        with self.assertRaises(history.HistoryUnavailable):
            self.recorder.summary(KEY)
        self.assertStoppedForCorruption(1800)
        with self.assertRaises(history.HistoryUnavailable):
            self.recorder.summary(KEY)

    def testCorruptionFoundByALevelsReadStopsTheHistory(self):
        self.event(0, 50)
        self.failQuery(corruption(), "from readings")
        with self.assertRaises(history.HistoryUnavailable):
            self.recorder.levels(KEY, "week")
        self.assertStoppedForCorruption(1800)
        with self.assertRaises(history.HistoryUnavailable):
            self.recorder.levels(KEY, "week")

    def testCorruptionFoundByAWriteDropsTheRowAndStopsTheHistory(self):
        self.event(0, 50)
        self.failQuery(corruption(), "insert into readings")
        self.event(1800, 49)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50)])
        self.assertFalse(self.recorder.entries[KEY].loaded)
        self.assertStoppedForCorruption(3600)

    def testCorruptionFoundLoadingAMouseStopsTheHistory(self):
        self.event(0, 50)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.failQuery(corruption(), "from mice")
        self.event(1800, 49)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50)])
        self.assertStoppedForCorruption(3600)

    def testAnErrorWithoutACodeIsntCorruption(self):
        db = sqlite3.connect(":memory:")
        db.close()
        with self.assertRaises(sqlite3.ProgrammingError) as caught:
            db.execute("select 1")
        self.assertFalse(history.isCorruption(caught.exception))

    def testAnExtendedCorruptionCodeCounts(self):
        error = sqlite3.DatabaseError("index")
        error.sqlite_errorcode = 779
        self.assertTrue(history.isCorruption(error))

    def testTenYearsOfTheWorstCaseStaysUnderTwentyFiveMegabytes(self):
        self.assertTrue(self.recorder.available())
        db = self.recorder.store.db
        rows = []
        steps = []
        at = START
        for day in range(3650):
            for i in range(120):
                at += 720
                wake = i >= 100
                rows.append((1, at, None if wake else i % 100, None, None if wake else 0, None if wake else 2, None, 2 if wake else 0))
                if (not wake):
                    steps.append((1, i % 2, at, 13.5, 50, 49, 2, 1, day))
        with self.recorder.store.transaction():
            db.execute("insert into mice (id, key, source, firstSeen) values (1, 'big', 'hidpp', 0)")
            db.executemany("insert into readings (mouseId, at, seq, percent, level, charging, power, millivolts, origin) values (?, ?, 0, ?, ?, ?, ?, ?, ?)", rows)
            db.executemany("insert into steps (mouseId, kind, at, seq, minutesPerPercent, fromPercent, toPercent, power, unbroken, session) values (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)", steps)
        db.execute("pragma wal_checkpoint(truncate)")
        self.assertLess(os.path.getsize(os.path.join(self.folder, history.FILE_NAME)), 25 * 1024 * 1024)


class TraceTests(RecorderCase):
    def testThePadChargeFromTheProbe(self):
        # 21% discharging off power, then the pad: charging with power 2 in the event that raised it to 22, and a step every 20 to 26 s
        self.event(0, 21)
        self.event(70, 22, charging=1, power=2)
        self.event(89, 23, charging=1, power=2)
        self.event(115, 24, charging=1, power=2)
        self.assertEqual(self.sessions(), [(0, 0, 0, 21, 21, 0, 1, 0, 0, 0), (1, 70, None, 22, None, 2, 3, 0, 0, 0)])
        self.assertEqual(self.steps(), [(1, 89, round(19 / 60, 3), 22, 23, 2, 1), (1, 115, round(26 / 60, 3), 23, 24, 2, 1)])

    def testLiftingOffThePadEndsTheChargeAtItsLastReading(self):
        self.event(0, 21)
        self.event(70, 22, charging=1, power=2)
        self.event(89, 23, charging=1, power=2)
        self.event(300, 22, charging=0, power=0)
        sessions = self.sessions()
        self.assertEqual(sessions[1][:5], (1, 70, 89, 22, 23))
        self.assertEqual(sessions[2][:5], (0, 300, None, 22, None))

    def testTheDischargeStepAndTheTwoWakes(self):
        self.event(0, 22)
        self.marker(600, history.ORIGIN_WAKE)
        self.event(600, 22)
        self.event(706, 21)
        self.marker(1278, history.ORIGIN_WAKE)
        self.event(1278, 21)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 22), (600, 2, None), (600, 0, 22), (706, 0, 21), (1278, 2, None), (1278, 0, 21)])
        # the 22 to 21 drop started at the wake set's reading, which came in at a level reached during the sleep
        self.assertEqual([step[6] for step in self.steps()], [0])

    def testAWakeSetsBatteryEventSentBeforeItsWakeStillCountsAsPartOfIt(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60)
        self.event(400, 59)
        self.clock.now = START + 2000
        self.recorder.addReading(KEY, battery(57), history.ORIGIN_EVENT, at=START + 2000)
        self.marker(2001, history.ORIGIN_WAKE)
        self.event(2400, 56)
        self.assertEqual([row[:3] for row in self.rows()][-3:], [(2001, 2, None), (2001, 0, 57), (2400, 0, 56)])
        # 59 to 57 spans the sleep, so it isn't whole, and 57 came in with the wake, so 57 to 56 isn't either
        self.assertEqual([(step[3], step[4], step[6]) for step in self.steps()], [(60, 59, 0), (59, 57, 0), (57, 56, 0)])


    def testAReadingWaitsTwoWholeSecondsFromItsArrival(self):
        self.assertTrue(self.recorder.available())
        self.clock.now = START + 100.9
        self.recorder.addReading(KEY, battery(57), history.ORIGIN_EVENT)
        self.clock.now = START + 102.0
        self.recorder.tick()
        self.assertEqual(self.rows(), [])
        self.clock.now = START + 102.5
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE)
        self.assertEqual([row[:3] for row in self.rows()], [(102, 2, None), (102, 0, 57)])

    def testAWakeLooksBackTwoSecondsFromItsOwnArrival(self):
        self.recorder.addReading(KEY, battery(57), history.ORIGIN_EVENT, at=START + 100.1)
        self.clock.now = START + 102.9
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE)
        self.assertEqual([row[:3] for row in self.rows()], [(100, 0, 57), (102, 2, None)])

class HoldTests(RecorderCase):
    def testAReadingInsideTenSecondsIsHeldAndWrittenWhenTheyreUp(self):
        self.event(0, 50)
        self.event(3, 49)
        self.assertEqual(len(self.rows()), 1)
        self.at(10)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (3, 0, 49)])

    def testALaterReadingReplacesTheHeldOne(self):
        self.event(0, 50)
        self.event(3, 49)
        self.event(5, 48)
        self.at(12)
        self.assertEqual([row[2] for row in self.rows()], [50, 48])

    def testAFlickerBackToTheSavedValueLeavesNothingBehind(self):
        self.event(0, 50)
        self.event(3, 49)
        self.event(5, 50)
        self.at(12)
        self.assertEqual([row[2] for row in self.rows()], [50])

    def testACodeChangeIsNeverHeld(self):
        self.event(0, 50)
        self.event(3, 50, charging=hidpp.CHARGING_ERROR)
        self.assertEqual([row[3] for row in self.rows()], [0, 4])

    def testACodeFourInsideAHoldIsSavedAndTheHeldRowGoesFirst(self):
        self.event(0, 50)
        self.event(3, 49)
        self.event(5, 49, charging=hidpp.CHARGING_ERROR)
        self.assertEqual([(row[2], row[3]) for row in self.rows()], [(50, 0), (49, 0), (49, 4)])

    def testAHeldRowGoesInBeforeAMarker(self):
        self.event(0, 50)
        self.event(3, 49)
        self.marker(6, history.ORIGIN_ASLEEP)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (3, 0, 49), (6, 3, None)])

    def testTheFirstReadingAfterAMarkerIsWrittenEvenUnchanged(self):
        self.event(0, 50)
        self.marker(600, history.ORIGIN_WAKE)
        self.event(601, 50)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (600, 2, None), (601, 0, 50)])

    def testAnUnchangedReadingWritesNothing(self):
        self.event(0, 50)
        self.event(400, 50, origin=history.ORIGIN_READ)
        self.assertEqual(len(self.rows()), 1)

    def testAVoltageNeedsTwentyMillivoltsToCount(self):
        self.event(0, None, power=None, millivolts=3900)
        self.event(400, None, power=None, millivolts=3890)
        self.event(800, None, power=None, millivolts=3878)
        with self.db() as db:
            self.assertEqual([mv for (mv,) in db.execute("select millivolts from readings")], [3900, 3878])

    def testStopWritesWhatsHeld(self):
        self.event(0, 50)
        self.event(3, 49)
        self.recorder.stop()
        self.assertEqual([row[2] for row in self.rows()], [50, 49])


class SessionTests(RecorderCase):
    def setUp(self):
        super().setUp()
        self.recorder.setWakeSupport(KEY, True)

    def testARiseOfTwoInfersACharge(self):
        self.event(0, 40)
        self.event(600, 42)
        self.assertEqual([(s[0], s[3], s[9]) for s in self.sessions()], [(0, 40, 0), (1, 42, 1)])

    def testTwoRisesOfOneInARowInferACharge(self):
        self.event(0, 40)
        self.event(600, 41)
        self.event(1200, 42)
        self.assertEqual([(s[0], s[3], s[9]) for s in self.sessions()], [(0, 40, 0), (1, 42, 1)])

    def testASingleRiseOfOneIsNoCharge(self):
        self.event(0, 40)
        self.event(600, 41)
        self.event(1200, 40)
        self.assertEqual([s[0] for s in self.sessions()], [0])

    def testARiseAcrossAWakeIsntCountedTowardACharge(self):
        self.event(0, 40)
        self.marker(600, history.ORIGIN_WAKE)
        self.event(601, 42)
        self.assertEqual([s[0] for s in self.sessions()], [0])

    def testAChargeDoneWhileAsleepIsSavedByItsEnds(self):
        self.event(0, 30)
        self.marker(600, history.ORIGIN_WAKE)
        self.event(5000, 80)
        self.assertEqual([(s[0], s[1], s[2], s[3], s[4], s[9]) for s in self.sessions()], [(0, 0, 0, 30, 30, 0), (1, 0, 5000, 30, 80, 1), (0, 5000, None, 80, None, 0)])

    def testAChargeAcrossAReboot(self):
        self.event(0, 30)
        self.marker(600, history.ORIGIN_GAP)
        self.event(30000, 90)
        self.assertEqual([(s[0], s[9]) for s in self.sessions()], [(0, 0), (1, 1), (0, 0)])

    def testAChargingCodeWinsOverTheFivePointRule(self):
        self.event(0, 40)
        self.marker(600, history.ORIGIN_GAP)
        self.event(5000, 60, charging=1, power=2)
        self.assertEqual([(s[0], s[3], s[4], s[9]) for s in self.sessions()], [(0, 40, 40, 0), (1, 60, None, 0)])

    def testFullEndsACharge(self):
        self.event(0, 98, charging=1, power=2)
        self.event(600, 100, charging=hidpp.CHARGING_FULL, power=2)
        self.assertEqual([(s[0], s[3], s[4]) for s in self.sessions()], [(1, 98, 98), (0, 100, None)])

    def testPowerGoingToZeroEndsACharge(self):
        self.event(0, 60, charging=1, power=2)
        self.event(600, 61, charging=1, power=0)
        self.assertEqual([s[0] for s in self.sessions()], [1, 0])

    def testAMissingPowerByteIsntAChangeOfPower(self):
        self.event(0, 60, charging=1, power=2)
        self.event(600, 61, charging=1, power=None)
        self.assertEqual([s[0] for s in self.sessions()], [1])

    def testAnInferredChargeEndsOnADropNotOnCodeZero(self):
        self.event(0, 40)
        self.event(600, 43)
        self.event(1200, 45)
        self.event(1800, 44)
        self.assertEqual([(s[0], s[3], s[4], s[9]) for s in self.sessions()], [(0, 40, 40, 0), (1, 43, 45, 1), (0, 44, None, 0)])

    def testAMouseWithoutWakeSupportNeedsFivePoints(self):
        self.recorder.setWakeSupport(KEY, False)
        self.event(0, 40)
        self.event(600, 42)
        self.event(1200, 41)
        self.event(1800, 46)
        self.assertEqual([(s[0], s[3]) for s in self.sessions()], [(0, 40), (1, 46)])

    def testAMouseWithoutWakeSupportNeedsThreeRisesOfOne(self):
        self.recorder.setWakeSupport(KEY, False)
        self.event(0, 40)
        self.event(600, 41)
        self.event(1200, 42)
        self.assertEqual([s[0] for s in self.sessions()], [0])
        self.event(1800, 43)
        self.assertEqual([s[0] for s in self.sessions()], [0, 1])

    def testSessionCountsSlowAndErrorReadings(self):
        self.event(0, 30, charging=hidpp.CHARGING_SLOW, power=1)
        self.event(600, 31, charging=hidpp.CHARGING_SLOW, power=1)
        self.event(1200, 32, charging=hidpp.CHARGING_ERROR, power=1)
        self.assertEqual(self.sessions()[0][5:9], (1, 3, 2, 1))

    def testALevelOnlyMouseGetsSessionsByCodeAndNoSteps(self):
        self.recorder.addReading(KEY, hidpp.BatteryReading(None, 0, level="good"), history.ORIGIN_EVENT, at=START)
        self.recorder.addReading(KEY, hidpp.BatteryReading(None, 1, level="good"), history.ORIGIN_EVENT, at=START + 600)
        self.at(700)
        self.assertEqual([s[0] for s in self.sessions()], [0, 1])
        self.assertEqual(self.steps(), [])


class StepTests(RecorderCase):
    def setUp(self):
        super().setUp()
        self.recorder.setWakeSupport(KEY, True)

    def drops(self, *pairs):
        for seconds, percent in pairs:
            self.event(seconds, percent)

    def testWholeDropsAreUnbroken(self):
        self.drops((0, 80), (1800, 79), (3600, 78), (5400, 77))
        self.assertEqual([(s[3], s[4], s[2], s[6]) for s in self.steps()], [(80, 79, 30.0, 0), (79, 78, 30.0, 1), (78, 77, 30.0, 1)])

    def testARestInsideAStepCostsOnlyThatStep(self):
        self.drops((0, 80), (1800, 79))
        self.marker(2000, history.ORIGIN_WAKE)
        self.drops((2001, 79), (3600, 78), (5400, 77))
        self.assertEqual([(s[3], s[4], s[6]) for s in self.steps()], [(80, 79, 0), (79, 78, 0), (78, 77, 1)])

    def testAReboundIsntANewLow(self):
        self.drops((0, 80), (1800, 79), (3600, 78), (4000, 79), (4400, 78), (6200, 77))
        # the second 78 isn't below the first, so neither drop from it counts, since each would start partway through a percent already passed
        self.assertEqual([(s[3], s[4], s[6]) for s in self.steps()], [(80, 79, 0), (79, 78, 1), (79, 78, 0), (78, 77, 0)])

    def testADropOnAChargerIsntUnbroken(self):
        self.event(0, 50, power=2)
        self.event(1800, 49, power=2)
        self.event(3600, 48, power=2)
        self.assertEqual([s[6] for s in self.steps()], [0, 0])

    def testAMouseWithoutWakeSupportHasNoUnbrokenDrops(self):
        self.recorder.setWakeSupport(KEY, False)
        self.drops((0, 80), (1800, 79), (3600, 78))
        self.assertEqual([s[6] for s in self.steps()], [0, 0])

    def testTheFirstRiseAfterAChargingCodeWithNoChangeIsntWhole(self):
        self.event(0, 30)
        self.event(600, 30, charging=1, power=2)
        self.event(900, 31, charging=1, power=2)
        self.event(1200, 32, charging=1, power=2)
        self.assertEqual([(s[3], s[4], s[6]) for s in self.steps()], [(30, 31, 0), (31, 32, 1)])

    def testAMultiPointStepIsMinutesPerPoint(self):
        self.drops((0, 80), (1800, 79), (3600, 76))
        self.assertEqual(self.steps()[-1][2:5], (10.0, 79, 76))

    def testAClockThatSteppedBackSavesNoStep(self):
        self.drops((5000, 80), (6800, 79), (100, 78))
        # stopping writes the held row, so the 79 to 78 step reaches the length check
        self.recorder.stop()
        self.assertIn((100, 0, 78), [row[:3] for row in self.rows()])
        self.assertEqual([(s[3], s[4]) for s in self.steps()], [(80, 79)])

    def testAClockThatSteppedBackWaitsNoLongerThanTwoHours(self):
        later = 40 * 24 * 60 * 60
        recorder = history.Recorder(None, self.clock)
        recorder.addReading(KEY, battery(50), history.ORIGIN_EVENT, at=START + later)
        self.clock.now = START + later + 2
        recorder.tick()
        recorder.addReading(KEY, battery(49), history.ORIGIN_EVENT, at=START + later + 3)
        self.clock.now = START + later + 5
        recorder.tick()
        self.assertIsNotNone(recorder.entries[KEY].held)
        self.clock.now = START
        recorder.tick()
        self.assertTrue(recorder.timer.isActive())
        self.assertLessEqual(recorder.timer.interval(), 2 * 60 * 60 * 1000 + 1)
        recorder.stop()


class ReportsItselfTests(RecorderCase):
    def setUp(self):
        super().setUp()
        self.recorder.setWakeSupport(KEY, True)

    def testADropAwayFromAWakeSetsTheFlag(self):
        self.marker(0, history.ORIGIN_WAKE)
        self.event(0, 60)
        self.event(1000, 59)
        self.assertTrue(self.recorder.reportsItself(KEY))
        with self.db() as db:
            self.assertEqual(db.execute("select reportsItselfSince - ? from mice", (START,)).fetchone()[0], 1000)

    def testADropRightAfterAWakeDoesnt(self):
        self.event(0, 60)
        self.marker(1000, history.ORIGIN_WAKE)
        self.event(1010, 59)
        self.assertFalse(self.recorder.reportsItself(KEY))

    def testARiseDoesnt(self):
        self.event(0, 60, charging=1, power=2)
        self.event(1000, 61, charging=1, power=2)
        self.assertFalse(self.recorder.reportsItself(KEY))

    def testAReadDoesnt(self):
        self.event(0, 60)
        self.event(1000, 59, origin=history.ORIGIN_READ)
        self.assertFalse(self.recorder.reportsItself(KEY))

    def testAMouseWithoutWakeSupportNeverQualifies(self):
        self.recorder.setWakeSupport(KEY, False)
        self.event(0, 60)
        self.event(1000, 59)
        self.assertFalse(self.recorder.reportsItself(KEY))

    def testAReadThatFindsTwoPointsMissedClearsIt(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.event(5000, 57, origin=history.ORIGIN_READ)
        self.assertFalse(self.recorder.reportsItself(KEY))
        with self.db() as db:
            self.assertIsNone(db.execute("select reportsItselfSince from mice").fetchone()[0])

    def testAClearThatFailsToSaveLeavesTheFlagAsSaved(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.clock.now = START + 5000
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.addReading(KEY, battery(57), history.ORIGIN_READ, at=START + 5000)
        with self.db() as db:
            saved = db.execute("select reportsItselfSince from mice").fetchone()[0]
        self.assertEqual(self.recorder.reportsItself(KEY), saved is not None)

    def testAWakeSupportThatFailsToSaveStaysAsSaved(self):
        self.event(0, 60)
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.setWakeSupport(KEY, False)
        with self.db() as db:
            saved = db.execute("select wakeSupport from mice").fetchone()[0]
        self.assertEqual(self.recorder.entries[KEY].track.wakeSupport, bool(saved))

    def testAReadAfterAMarkerIsntCompared(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.marker(2000, history.ORIGIN_GAP)
        self.event(9000, 40, origin=history.ORIGIN_READ)
        self.assertTrue(self.recorder.reportsItself(KEY))

    def testTheFlagSurvivesARestart(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.recorder.setWakeSupport(KEY, True)
        self.assertTrue(self.recorder.reportsItself(KEY))

    def testAPathKeyMouseKeepsItsFlagInMemoryOnly(self):
        key = "/dev/hidraw4#1"
        self.recorder.setWakeSupport(key, True, pathKey=True)
        self.recorder.addReading(key, battery(60), history.ORIGIN_EVENT, pathKey=True, at=START)
        self.recorder.addReading(key, battery(59), history.ORIGIN_EVENT, pathKey=True, at=START + 1000)
        self.at(1100)
        self.assertTrue(self.recorder.reportsItself(key))
        with self.db() as db:
            self.assertEqual(db.execute("select count(*) from mice").fetchone()[0], 1)


    def testAMouseWithoutAHistorySaysWhenItsFlagChanges(self):
        key = "/dev/hidraw4#1"
        changed = []
        self.recorder.changed.connect(changed.append)
        self.recorder.setWakeSupport(key, True, pathKey=True)
        self.recorder.addReading(key, battery(60), history.ORIGIN_EVENT, pathKey=True, at=START)
        self.at(500)
        self.assertEqual(changed, [])
        self.recorder.addReading(key, battery(59), history.ORIGIN_EVENT, pathKey=True, at=START + 1000)
        self.at(1100)
        self.assertEqual(changed, [key])

class RecorderTests(RecorderCase):
    def testUPowerRowsForAHidppMouseAreLeftOut(self):
        self.event(0, 50)
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual([row[2] for row in self.rows()], [50])

    def testUPowerRowsForAKeyTheSavedHistoryCallsHidppAreLeftOut(self):
        self.event(0, 50)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual([row[2] for row in self.rows()], [50])

    def testAUPowerRowQueuedBeforeTheHidppSearchIsLeftOut(self):
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.clock.now = START + 1
        self.recorder.setWakeSupport(KEY, True)
        self.recorder.addMarker(KEY, history.ORIGIN_GAP, at=START + 1)
        self.event(1, 50)
        self.at(600)
        self.assertEqual([row[1] for row in self.rows()], [history.ORIGIN_GAP, history.ORIGIN_EVENT])

    def testAHidppClaimAfterSavedUPowerRowsSavesTheSourceAsHidpp(self):
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.at(10)
        self.recorder.setWakeSupport(KEY, True)
        with self.db() as db:
            self.assertEqual(db.execute("select source from mice").fetchone()[0], history.SOURCE_HIDPP)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.recorder.addReading(KEY, battery(48), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual([row[2] for row in self.rows()], [49])

    def testASourceThatFailedToSaveIsSavedWhenTheMouseLoadsAgain(self):
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.at(10)
        real = self.recorder.store.transaction
        calls = []

        def failingOnce():
            calls.append(1)
            if (len(calls) == 1):
                raise sqlite3.OperationalError("database or disk is full")
            return real()
        with mock.patch.object(self.recorder.store, "transaction", failingOnce):
            self.recorder.setWakeSupport(KEY, True)
        with self.db() as db:
            self.assertEqual(db.execute("select source from mice").fetchone()[0], history.SOURCE_HIDPP)

    def testAUPowerOnlyMouseGetsAHistory(self):
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.at(10)
        with self.db() as db:
            self.assertEqual(db.execute("select key, source from mice").fetchall(), [("9a1b2c3d", "upower")])

    def testAPathKeyMouseGetsNoHistory(self):
        self.assertTrue(self.recorder.available())
        self.recorder.addReading("/dev/hidraw4#1", battery(70), history.ORIGIN_EVENT, pathKey=True, at=START)
        self.at(10)
        with self.db() as db:
            self.assertEqual(db.execute("select count(*) from mice").fetchone()[0], 0)

    def testANameThatComesAfterTheRowIsMadeIsSaved(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 50)
        with self.db() as db:
            self.assertEqual(db.execute("select name from mice").fetchone()[0], "PRO X3 SUPERSTRIKE")

    def testEachSavedRowMovesTheRevisionAndSaysWhichMouse(self):
        changed = []
        self.recorder.changed.connect(changed.append)
        self.event(0, 50)
        self.marker(600, history.ORIGIN_WAKE)
        self.assertEqual((self.recorder.revision(KEY), changed), (2, [KEY, KEY]))

    def testAFailedWriteIsRolledBackAndTheNextRowFollowsAGap(self):
        self.event(0, 50)
        real = self.recorder.store.transaction
        calls = []

        def failingOnce():
            calls.append(1)
            if (len(calls) == 1):
                raise sqlite3.OperationalError("database or disk is full")
            return real()
        with mock.patch.object(self.recorder.store, "transaction", failingOnce):
            self.event(1800, 49)
        self.assertEqual(self.recorder.entries[KEY].track.last.percent, 50)
        self.event(3600, 48)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (3600, 4, None), (3600, 0, 48)])
        self.assertIn("database or disk is full", self.errors.getvalue())

    def testASaveFailureThatComesBackAfterARecoveryIsPrintedAgain(self):
        self.event(0, 50)
        for seconds, percent in ((1800, 49), (5400, 47)):
            with self.failOnce(sqlite3.OperationalError("database or disk is full")):
                self.event(seconds, percent)
            self.event(seconds + 1800, percent - 1)
        self.assertEqual(self.errors.getvalue().splitlines(), ["Couldn't save to the history: database or disk is full", "The history is saving again"] * 2)

    def testARestartReloadsTheOpenSession(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60, charging=1, power=2)
        self.event(600, 61, charging=1, power=2)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.event(1200, 61, charging=0, power=0)
        self.assertEqual([(s[0], s[1], s[2]) for s in self.sessions()], [(1, 0, 600), (0, 1200, None)])
        self.assertEqual([row[1] for row in self.rows()][-2:], [4, 0])

    def testAFailedHidppLookupForAUPowerReadingDropsItAndAsksAgain(self):
        real = self.failQuery(sqlite3.OperationalError("disk I/O error"))
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        entry = self.recorder.entries["9a1b2c3d"]
        self.assertEqual((entry.source, entry.pending), (None, []))
        self.assertIn("Couldn't read the history: disk I/O error", self.errors.getvalue())
        self.recorder.store.db = real
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 60)
        self.assertEqual((entry.source, len(entry.pending)), (history.SOURCE_UPOWER, 1))

    def testACorruptHidppLookupForAUPowerReadingMarksTheHistoryCorrupt(self):
        self.failQuery(corruption())
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.assertTrue(self.recorder.store.brokenThisRun)
        self.assertFalse(self.recorder.available())

    def testAReloadedVoltageOnlySessionEndsAtItsLastReading(self):
        self.event(0, None, power=None, millivolts=3900)
        self.event(400, None, power=None, millivolts=3850)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False)
        self.event(1200, None, charging=1, power=None, millivolts=4000)
        self.assertEqual([(s[0], s[1], s[2]) for s in self.sessions()], [(0, 0, 400), (1, 1200, None)])

    def testWithNoFolderTheRulesStillRunInMemory(self):
        recorder = history.Recorder(None, self.clock, timers=False)
        recorder.setWakeSupport(KEY, True)
        recorder.addReading(KEY, battery(60), history.ORIGIN_EVENT, at=START)
        recorder.addReading(KEY, battery(59), history.ORIGIN_EVENT, at=START + 1000)
        self.clock.now = START + 1100
        recorder.tick()
        self.assertTrue(recorder.reportsItself(KEY))
        self.assertFalse(recorder.available())
        recorder.stop()



class SummaryTests(RecorderCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.recorder.available())
        self.database = self.recorder.store.db
        with self.recorder.store.transaction():
            self.database.execute("insert into mice (id, key, name, source, firstSeen, wakeSupport) values (1, ?, 'PRO X3 SUPERSTRIKE', 'hidpp', ?, 1)", (KEY, START))

    def addSession(self, kind, startAt, endAt=None, startPercent=80, endPercent=20, power=0, readingCount=10, slowCount=0, errorCount=0, inferred=0):
        with self.recorder.store.transaction():
            cursor = self.database.execute("insert into sessions (mouseId, kind, startAt, endAt, startPercent, endPercent, power, readingCount, slowCount, errorCount, inferred) values (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (kind, START + startAt, None if endAt is None else START + endAt, startPercent, endPercent, power, readingCount, slowCount, errorCount, inferred))
        return cursor.lastrowid

    def addStep(self, session, kind, at, minutes, fromPercent, toPercent, unbroken=1, power=0):
        with self.recorder.store.transaction():
            self.database.execute("insert into steps (mouseId, kind, at, seq, minutesPerPercent, fromPercent, toPercent, power, unbroken, session) values (1, ?, ?, (select coalesce(max(seq) + 1, 0) from steps where mouseId = 1 and kind = ? and at = ?), ?, ?, ?, ?, ?, ?)", (kind, START + at, kind, START + at, minutes, fromPercent, toPercent, power, unbroken, session))

    def addReadingRow(self, at, origin=history.ORIGIN_EVENT, percent=50, charging=0):
        with self.recorder.store.transaction():
            self.database.execute("insert into readings (mouseId, at, seq, percent, charging, power, origin) values (1, ?, (select coalesce(max(seq) + 1, 0) from readings where mouseId = 1 and at = ?), ?, ?, 0, ?)", (START + at, START + at, None if origin in history.MARKER_ORIGINS else percent, None if origin in history.MARKER_ORIGINS else charging, origin))

    def discharges(self, count, minutes, fromPercent=75):
        for i in range(count):
            session = self.addSession(history.DISCHARGE, i * 1000, i * 1000 + 900)
            for j in range(3):
                self.addStep(session, history.DISCHARGE, i * 1000 + j + 1, minutes(i), fromPercent - j, fromPercent - j - 1)

    def testAnUnknownKeyIsAnEmptyHistory(self):
        summary = self.recorder.summary("ffffffff")
        self.assertEqual((summary["key"], summary["sessions"], summary["open"], summary["charge"]), ("ffffffff", [], None, []))

    def testAKeyFromAnyAccountIsOnlyEverAValue(self):
        # any local account may send any key, so it's bound as a parameter and never read as SQL
        for key in ("x' or '1'='1", "'; drop table readings; --", "k" * 100000, ""):
            self.assertEqual(self.recorder.summary(key)["sessions"], [])
        with self.recorder.store.transaction():
            self.assertEqual(self.database.execute("select count(*) from mice").fetchone()[0], 1)

    def testNoHistoryFolderMeansUnavailable(self):
        recorder = history.Recorder(None, self.clock, timers=False)
        with self.assertRaises(history.HistoryUnavailable):
            recorder.summary(KEY)

    def testTheReplyIsPlainJson(self):
        self.discharges(3, lambda i: 30.0)
        import json
        self.assertEqual(json.loads(json.dumps(self.recorder.summary(KEY)))["key"], KEY)

    def testABucketNeedsThreeDropsFromASessionToCountIt(self):
        session = self.addSession(history.DISCHARGE, 0, 900)
        self.addStep(session, history.DISCHARGE, 1, 30.0, 75, 74)
        self.addStep(session, history.DISCHARGE, 2, 30.0, 74, 73)
        bucket = self.recorder.summary(KEY)["drain"]["buckets"][0]
        self.assertEqual((bucket["bucket"], bucket["sessions"]), (80, 0))

    def testABucketIsReadyAtFortyTwoSessions(self):
        self.discharges(41, lambda i: 30.0)
        bucket = self.recorder.summary(KEY)["drain"]["buckets"][0]
        self.assertEqual((bucket["sessions"], bucket["first"], bucket["last"]), (41, 30.0, None))
        self.discharges(1, lambda i: 30.0)
        self.assertEqual(self.recorder.summary(KEY)["drain"]["buckets"][0]["last"], [30.0, 30.0, 30.0])

    def testTheBaselineIsTheFirstTwentyAndTheWindowsEndAtTheNewestThree(self):
        self.discharges(42, lambda i: float(i))
        bucket = self.recorder.summary(KEY)["drain"]["buckets"][0]
        # medians of 0..19, 22..41, 21..40, and 20..39
        self.assertEqual((bucket["first"], bucket["last"]), (9.5, [31.5, 30.5, 29.5]))

    def testDropsGoToTheBucketTheirStartFallsIn(self):
        for fromPercent, bucket in ((80, 80), (71, 80), (70, 70), (21, 30)):
            self.assertEqual(history.drainBucket(fromPercent), bucket)
        for fromPercent, bucket in ((20, 20), (29, 20), (79, 70)):
            self.assertEqual(history.chargeBucket(fromPercent), bucket)
        self.assertEqual((history.drainBucket(81), history.drainBucket(20), history.chargeBucket(80), history.chargeBucket(19)), (None, None, None, None))

    def testADropThatEndsBelowTheBandIsLeftOut(self):
        session = self.addSession(history.DISCHARGE, 0, 900)
        for j in range(3):
            self.addStep(session, history.DISCHARGE, j + 1, 30.0, 21, 19)
        self.assertEqual(self.recorder.summary(KEY)["drain"]["buckets"][-1]["sessions"], 0)

    def testBrokenDropsAreLeftOut(self):
        session = self.addSession(history.DISCHARGE, 0, 900)
        for j in range(3):
            self.addStep(session, history.DISCHARGE, j + 1, 30.0, 75 - j, 74 - j, unbroken=0)
        self.assertEqual(self.recorder.summary(KEY)["drain"]["buckets"][0]["sessions"], 0)

    def testChargeFiguresAreKeptPerKindOfPower(self):
        for power in (2, 1):
            session = self.addSession(history.CHARGE, power * 10000, power * 10000 + 900, 20, 80, power=power)
            for j in range(3):
                self.addStep(session, history.CHARGE, power * 10000 + j + 1, 0.5 * power, 25 + j, 26 + j)
        charge = self.recorder.summary(KEY)["charge"]
        self.assertEqual([(entry["power"], entry["buckets"][0]["sessions"]) for entry in charge], [(1, 1), (2, 1)])

    def testLargeStepsNeedsTwentyDropsOfFiveOrMore(self):
        session = self.addSession(history.DISCHARGE, 0)
        for j in range(19):
            self.addStep(session, history.DISCHARGE, j + 1, 60.0, 100 - 5 * j, 95 - 5 * j, unbroken=0)
        self.assertFalse(self.recorder.summary(KEY)["drain"]["largeSteps"])
        self.addStep(session, history.DISCHARGE, 30, 60.0, 5, 0, unbroken=0)
        self.assertTrue(self.recorder.summary(KEY)["drain"]["largeSteps"])

    def testTheOpenSessionGivesItsMedianAtAnyLevel(self):
        session = self.addSession(history.DISCHARGE, 0)
        for j, minutes in enumerate((20.0, 30.0, 40.0)):
            self.addStep(session, history.DISCHARGE, j + 1, minutes, 95 - j, 94 - j)
        self.assertEqual(self.recorder.summary(KEY)["open"], {"kind": history.DISCHARGE, "startAt": START, "startPercent": 80, "median": 30.0, "count": 3})

    def testNoQueryForASummaryOrALevelsReadScansATable(self):
        self.addSession(history.DISCHARGE, 0)
        statements = []
        self.database.set_trace_callback(statements.append)
        self.recorder.summary(KEY)
        for name in history.LEVEL_RANGES:
            self.recorder.levels(KEY, name)
        self.database.set_trace_callback(None)
        self.assertTrue(any("from steps" in sql for sql in statements))
        scans = [sql for sql in dict.fromkeys(statements) if any(row[3].startswith("SCAN") for row in self.database.execute("explain query plan " + sql))]
        self.assertEqual(scans, [])

    def testLastChargeAndLastError(self):
        self.addSession(history.CHARGE, 0, 500, 20, 80, power=2, readingCount=6, slowCount=4)
        self.addSession(history.DISCHARGE, 500)
        self.addReadingRow(700, charging=hidpp.CHARGING_ERROR)
        summary = self.recorder.summary(KEY)
        self.assertEqual((summary["lastCharge"]["startAt"], summary["lastCharge"]["slowCount"], summary["lastError"]), (START, 4, START + 700))

    def testMonthlyFiguresGroupByUtcMonth(self):
        nextMonth = 31 * 24 * 60 * 60
        first = self.addSession(history.DISCHARGE, 0, 1000, 80, 30)
        for j in range(3):
            self.addStep(first, history.DISCHARGE, j + 1, 30.0, 75 - j, 74 - j)
        self.addSession(history.DISCHARGE, nextMonth, nextMonth + 1000, 90, 40)
        monthly = self.recorder.summary(KEY)["monthly"]
        self.assertEqual([(month["drain"], month["drained"], month["sessions"]) for month in monthly], [(30.0, 50, 1), (None, 50, 1)])
        self.assertEqual(monthly[0]["month"], history.monthOf(START))

    def testSessionsAreTheLastNinetyDaysOldestFirst(self):
        self.clock.now = START + 100 * 24 * 60 * 60
        self.addSession(history.DISCHARGE, 0, 100)
        self.addSession(history.CHARGE, 95 * 24 * 60 * 60, None, 20, None)
        self.addSession(history.DISCHARGE, 96 * 24 * 60 * 60, None)
        sessions = self.recorder.summary(KEY)["sessions"]
        self.assertEqual([row[0] for row in sessions], [history.CHARGE, history.DISCHARGE])
        self.assertEqual(len(sessions[0]), len(history.SESSION_COLUMNS))

    def testRecentHasEightyFourSlotsCarriedForward(self):
        self.clock.now = START + 8 * 24 * 60 * 60
        self.addReadingRow(0, percent=90)
        self.addReadingRow(7 * 24 * 60 * 60 + 600, percent=70)
        recent = self.recorder.summary(KEY)["recent"]
        self.assertEqual(len(recent), 84)
        self.assertEqual((recent[0][1], recent[-1][1]), (90, 70))

    def testASlotAfterAGapIsEmptyUntilTheNextReading(self):
        self.clock.now = START + 7 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        self.addReadingRow(3 * 60 * 60, origin=history.ORIGIN_GAP)
        self.addReadingRow(9 * 60 * 60, percent=85)
        values = [slot[1] for slot in self.recorder.summary(KEY)["recent"]]
        self.assertEqual(values[:6], [90, None, None, None, 85, 85])

    def testASlotInsideAChargeIsMarkedCharged(self):
        self.clock.now = START + 7 * 24 * 60 * 60
        self.addReadingRow(60, percent=30)
        self.addSession(history.CHARGE, 3 * 60 * 60, 5 * 60 * 60, 30, 80)
        charged = [slot[4] for slot in self.recorder.summary(KEY)["recent"]]
        self.assertEqual(charged[:4], [False, True, True, False])


    def addRoughRow(self, at, level=None, millivolts=None):
        with self.recorder.store.transaction():
            self.database.execute("insert into readings (mouseId, at, seq, level, millivolts, charging, power, origin) values (1, ?, 0, ?, ?, 0, 0, ?)", (START + at, level, millivolts, history.ORIGIN_EVENT))

    def testEachRangeEndsNowWithItsOwnSlotCount(self):
        self.clock.now = START + 400 * 24 * 60 * 60
        for name, count, seconds in (("day", 48, 30 * 60), ("week", 84, 2 * 60 * 60), ("month", 60, 12 * 60 * 60), ("halfYear", 26, 7 * 24 * 60 * 60), ("year", 52, 7 * 24 * 60 * 60)):
            levels = self.recorder.levels(KEY, name)
            self.assertEqual((levels["version"], levels["key"], levels["range"]), (1, KEY, name))
            ends = [slot[0] for slot in levels["slots"]]
            self.assertEqual((len(ends), ends[-1], ends[1] - ends[0]), (count, self.clock.now, seconds), name)

    def testTheWeekIsTheHistorysRecentSlots(self):
        self.clock.now = START + 8 * 24 * 60 * 60
        self.addReadingRow(0, percent=90)
        self.addReadingRow(7 * 24 * 60 * 60 + 600, percent=70)
        self.assertEqual(self.recorder.levels(KEY, "week")["slots"], self.recorder.summary(KEY)["recent"])

    def testADayStartsEmptyAfterAGapBeforeIt(self):
        # a mouse gone since before the day began shows no level until it reads again
        self.clock.now = START + 48 * 60 * 60
        self.addReadingRow(60, percent=50)
        self.addReadingRow(18 * 60 * 60, origin=history.ORIGIN_GAP)
        self.addReadingRow(43 * 60 * 60, percent=70)
        slots = self.recorder.levels(KEY, "day")["slots"]
        self.assertEqual([slot[1] for slot in slots[:2]], [None, None])
        self.assertEqual(slots[-1][1], 70)

    def testTheDayHasHalfHourLevelBars(self):
        self.clock.now = START + 24 * 60 * 60
        self.addReadingRow(10, percent=80)
        self.addReadingRow(45 * 60, percent=78)
        slots = self.recorder.levels(KEY, "day")["slots"]
        self.assertEqual([slot[1] for slot in slots[:3]], [80, 78, 78])

    def testALongSlotSpansItsLowestToHighest(self):
        self.clock.now = START + 30 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        self.addReadingRow(3 * 60 * 60, percent=70)
        self.addReadingRow(6 * 60 * 60, percent=60)
        slots = self.recorder.levels(KEY, "month")["slots"]
        # the second slot holds no reading, so it carries the last level as a flat bar
        self.assertEqual([slot[1:3] for slot in slots[:2]], [[60, 90], [60, 60]])

    def testALongSlotStartsFromTheLevelCarriedIntoIt(self):
        self.clock.now = START + 30 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        self.addReadingRow(13 * 60 * 60, percent=80)
        slots = self.recorder.levels(KEY, "month")["slots"]
        self.assertEqual(slots[1][1:3], [80, 90])

    def testALongSlotCountsTheChargesStartedInIt(self):
        self.clock.now = START + 30 * 24 * 60 * 60
        self.addReadingRow(60, percent=30)
        self.addSession(history.CHARGE, 2 * 60 * 60, 3 * 60 * 60, 30, 80)
        self.addSession(history.CHARGE, 5 * 60 * 60, 6 * 60 * 60, 70, 90)
        slots = self.recorder.levels(KEY, "month")["slots"]
        self.assertEqual([slot[3] for slot in slots[:2]], [2, 0])

    def testALongSlotAfterAGapIsEmptyUntilTheNextReading(self):
        self.clock.now = START + 30 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        self.addReadingRow(6 * 60 * 60, origin=history.ORIGIN_GAP)
        self.addReadingRow(37 * 60 * 60, percent=85)
        slots = self.recorder.levels(KEY, "month")["slots"]
        self.assertEqual([slot[1:3] for slot in slots[:4]], [[90, 90], [None, None], [None, None], [85, 85]])

    def testAllHasOneSlotPerLocalMonthSinceTheMouseWasFirstSeen(self):
        self.clock.now = START + 70 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        slots = self.recorder.levels(KEY, "all")["slots"]
        first = time.localtime(START)
        last = time.localtime(self.clock.now)
        months = (last.tm_year - first.tm_year) * 12 + last.tm_mon - first.tm_mon + 1
        self.assertEqual(len(slots), months)
        self.assertEqual(slots[0][0], time.mktime((first.tm_year + first.tm_mon // 12, first.tm_mon % 12 + 1, 1, 0, 0, 0, 0, 0, -1)))
        self.assertEqual((slots[-1][0], slots[0][1:3]), (self.clock.now, [90, 90]))

    def testAllKeepsTheLastHundredTwentyMonths(self):
        self.clock.now = START + 11 * 366 * 24 * 60 * 60
        self.assertEqual(len(self.recorder.levels(KEY, "all")["slots"]), 120)

    def testRoughLevelsAndMillivoltsBecomeHeights(self):
        self.clock.now = START + 30 * 24 * 60 * 60
        self.addRoughRow(60, level="good")
        self.addRoughRow(13 * 60 * 60, millivolts=3850)
        slots = self.recorder.levels(KEY, "month")["slots"]
        self.assertEqual([slot[1:3] for slot in slots[:2]], [[hidpp.LEVEL_HEIGHTS["good"], hidpp.LEVEL_HEIGHTS["good"]], [hidpp.LEVEL_HEIGHTS["good"], 3850]])

    def testAnUnknownRangeIsRefusedAndAnUnknownKeyIsEmpty(self):
        with self.assertRaises(ValueError):
            self.recorder.levels(KEY, "decade")
        self.assertEqual(self.recorder.levels("ffffffff", "year")["slots"], [])
        with self.assertRaises(history.HistoryUnavailable):
            history.Recorder(None, self.clock, timers=False).levels(KEY, "year")

if (__name__ == "__main__"):
    unittest.main()
