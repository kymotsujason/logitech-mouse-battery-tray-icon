import contextlib
import gc
import io
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import hidpp
import history

app = QApplication.instance() or QApplication([])
KEY = "02bc524c"
OTHER = "0badc0de"
START = 1800000000
BOOT = "test-boot"
FIXTURE_SCHEMA = (
    # the schema as fac27f9 wrote it, kept here so a later change to history.SCHEMA can't change what this opens
    "create table mice (id integer primary key, key text not null unique, name text, source text not null, firstSeen integer not null, reportsItselfSince integer, wakeSupport integer not null default 0)",
    "create table readings (mouseId integer not null, at integer not null, seq integer not null, percent integer, level text, charging integer, power integer, millivolts integer, origin integer not null, primary key (mouseId, at, seq)) without rowid",
    "create table steps (mouseId integer not null, kind integer not null, at integer not null, seq integer not null, minutesPerPercent real not null, fromPercent integer not null, toPercent integer not null, power integer, unbroken integer not null, session integer not null, primary key (mouseId, kind, at, seq)) without rowid",
    "create table sessions (id integer primary key, mouseId integer not null, kind integer not null, startAt integer not null, endAt integer, startPercent integer, endPercent integer, power integer, readingCount integer not null, slowCount integer not null, errorCount integer not null, inferred integer not null)",
    "create index sessionsByMouse on sessions (mouseId, startAt)",
)


class Clock:
    # the wall clock a test sets, a monotonic one that moves forward with it but stays put when it steps back or the machine sleeps, and a boot clock that counts a sleep too
    def __init__(self, now=START):
        self.wall = now
        self.steady = 0.0
        self.boot = 0.0

    @property
    def now(self):
        return self.wall

    @now.setter
    def now(self, value):
        forward = max(0, value - self.wall)
        self.steady += forward
        self.boot += forward
        self.wall = value

    def __call__(self):
        return self.wall

    def monotonic(self):
        return self.steady

    def boottime(self):
        return self.boot

    def jump(self, seconds):
        # a correction of the wall clock alone, which the monotonic and boot clocks never see
        self.wall += seconds

    def suspend(self, seconds):
        # the wall and boot clocks run on through a suspend while the monotonic one stops
        self.wall += seconds
        self.boot += seconds


def battery(percent, charging=0, power=0, level=None, millivolts=None):
    return hidpp.BatteryReading(percent, charging, millivolts, level, power)


def waitUntil(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        if (condition()):
            return True
        QTest.qWait(20)
    return condition()


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
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)

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

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(os.path.join(self.folder, history.FILE_NAME))
        try:
            # the connection's own with block commits, but it never closes
            with db:
                yield db
        finally:
            db.close()

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

    @contextlib.contextmanager
    def locked(self):
        # another process holding a write transaction on the file, which SQLite answers with SQLITE_BUSY once its busy wait runs out
        other = sqlite3.connect(os.path.join(self.folder, history.FILE_NAME), isolation_level=None)
        try:
            other.execute("begin immediate")
            yield
        finally:
            other.close()

    def failOpening(self):
        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        self.recorder.store.closeQuietly()
        return mock.patch.object(sqlite3, "connect", failing)

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
        self.assertEqual(tables, {"mice", "readings", "steps", "sessions", "heartbeat", "boots"})

    @unittest.skipUnless(sys.version_info >= (3, 13), "sqlite3 warns about unclosed connections only from Python 3.13")
    def testTheHelpersCloseTheirConnections(self):
        self.event(0, 50)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.rows()
            self.sessions()
            self.steps()
            with self.db() as db:
                db.execute("select 1")
            gc.collect()
        self.assertEqual([str(each.message) for each in caught if issubclass(each.category, ResourceWarning)], [])

    def testTheDatabaseIsInWalMode(self):
        self.assertTrue(self.recorder.available())
        with self.db() as db:
            self.assertEqual(db.execute("pragma journal_mode").fetchone()[0], "wal")

    def testEachCommitReachesTheDiskBeforeItsDone(self):
        # synchronous is a setting of the connection, so it's read from the recorder's own
        self.assertTrue(self.recorder.available())
        self.assertEqual(self.recorder.store.db.execute("pragma synchronous").fetchone()[0], 2)

    def testADatabaseWrittenByThisCodeOpensAgain(self):
        self.event(0, 50)
        self.recorder.stop()
        again = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.assertTrue(again.available())
        again.stop()
        self.assertEqual([name for name in os.listdir(self.folder) if ".broken-" in name], [])
        # the first run's stop wrote no gap row, so the next start adds one where it was last seen
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (2, history.ORIGIN_GAP, None)])

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
        again = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.assertEqual(self.checksOpening(again), ["pragma integrity_check"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        again.stop()
        self.assertEqual([name for name in os.listdir(self.folder) if ".broken-" in name], [])
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (2, history.ORIGIN_GAP, None)])
        # the flag is gone, so the start after that goes back to the quick check
        last = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.assertEqual(self.checksOpening(last), ["pragma quick_check"])
        last.stop()

    def checksOpening(self, recorder):
        statements = []
        real = sqlite3.connect

        def tracing(*args, **kwargs):
            db = real(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with mock.patch.object(sqlite3, "connect", tracing):
            self.assertTrue(recorder.available())
        return [sql for sql in statements if sql in ("pragma integrity_check", "pragma quick_check")]

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
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.failQuery(corruption(), "from mice")
        self.event(1800, 49)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (2, history.ORIGIN_GAP, None)])
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
        # these came in the charge's first 2 minutes, which a cable charge on 2026-10-09 showed can rise faster than the battery fills, so they aren't measured even though these two were real
        self.assertEqual(self.steps(), [(1, 89, round(19 / 60, 3), 22, 23, 2, 0), (1, 115, round(26 / 60, 3), 23, 24, 2, 0)])

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
        self.clock.now = START + 100.1
        self.recorder.addReading(KEY, battery(57), history.ORIGIN_EVENT)
        self.clock.now = START + 102.9
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE)
        self.assertEqual([row[:3] for row in self.rows()], [(100, 0, 57), (102, 2, None)])

    def testAReadingStampedBeforeAWakeSetStaysOutOfIt(self):
        self.assertTrue(self.recorder.available())
        self.clock.now = START + 100
        self.recorder.addReading(KEY, battery(57), history.ORIGIN_EVENT, arrived=self.clock.monotonic() - 2.5)
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE, arrived=self.clock.monotonic())
        self.assertEqual([row[:3] for row in self.rows()], [(100, 0, 57), (100, 2, None)])

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

    def testAWakeSetsReadingDoesntShortenTheNextHold(self):
        self.event(0, 50)
        self.clock.now = START + 100
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_EVENT)
        self.clock.now = START + 101
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE)
        self.clock.now = START + 104
        self.recorder.addReading(KEY, battery(48), history.ORIGIN_EVENT)
        self.at(106)
        self.assertIsNotNone(self.recorder.entries[KEY].held)
        # the hold counts from the wake row's write, not from the earlier arrival of the reading saved with it
        self.at(110.5)
        self.assertIsNotNone(self.recorder.entries[KEY].held)
        self.at(111)
        self.assertIsNone(self.recorder.entries[KEY].held)

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


class TimerTests(RecorderCase):
    def testTheRecordersOwnTimerWritesAWaitingRowAndThenAHeldOne(self):
        self.recorder = history.Recorder(self.folder)
        self.assertTrue(self.recorder.available())
        with mock.patch.object(history, "WAKE_SET_SECONDS", 0.2), mock.patch.object(history, "HOLD_SECONDS", 2):
            self.recorder.addReading(KEY, battery(50), history.ORIGIN_EVENT)
            self.assertTrue(waitUntil(lambda: len(self.rows()) == 1))
            self.recorder.addReading(KEY, battery(49), history.ORIGIN_EVENT)
            entry = self.recorder.entries[KEY]
            self.assertTrue(waitUntil(lambda: entry.held is not None))
            self.assertEqual(len(self.rows()), 1)
            self.assertTrue(waitUntil(lambda: len(self.rows()) == 2))
        self.assertEqual([row[2] for row in self.rows()], [50, 49])

    def testTheTimerWaitsOnTheMonotonicClock(self):
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, monotonic=self.clock.monotonic)
        self.clock.now = START + 100
        self.recorder.addReading(KEY, battery(50), history.ORIGIN_EVENT)
        self.assertAlmostEqual(self.recorder.timer.interval(), history.WAKE_SET_SECONDS * 1000, delta=5)
        self.clock.now = START - 2 * 60 * 60
        self.recorder.schedule()
        self.assertAlmostEqual(self.recorder.timer.interval(), history.WAKE_SET_SECONDS * 1000, delta=5)

    def testAnOpenThatFailsAtTheStartIsRetriedOnTheTimerWithNoMouse(self):
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, monotonic=self.clock.monotonic)
        with self.failOpening():
            self.assertFalse(self.recorder.available())
        self.assertTrue(self.recorder.timer.isActive())
        self.assertAlmostEqual(self.recorder.timer.interval(), history.OPEN_RETRY_SECONDS * 1000, delta=5)
        self.clock.now += history.OPEN_RETRY_SECONDS
        self.recorder.tick()
        self.assertTrue(self.recorder.store.isOpen())


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

    def testAMouseWhoseWakesArentSeenNeedsFivePoints(self):
        # saved with wake support, but the receiver hasn't found the index this run
        self.recorder.setWakeSupport(KEY, None)
        self.event(0, 40)
        self.event(600, 42)
        self.assertEqual([s[0] for s in self.sessions()], [0])
        self.event(1200, 47)
        self.assertEqual([(s[0], s[3]) for s in self.sessions()], [(0, 40), (1, 47)])

    def testAMouseWhoseWakesArentSeenNeedsThreeRisesOfOne(self):
        self.recorder.setWakeSupport(KEY, None)
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

    def testNoDropIsUnbrokenWhileTheReceiverCantSeeWakes(self):
        self.recorder.setWakeSupport(KEY, None)
        self.drops((0, 80), (1800, 79), (3600, 78))
        self.assertEqual((self.recorder.reportsItself(KEY), self.recorder.summary(KEY)["wakeSupport"]), (False, True))
        # the step in progress when the index comes back may hold a sleep nothing saw
        self.recorder.setWakeSupport(KEY, True)
        self.drops((5400, 77), (7200, 76))
        self.assertEqual([(s[3], s[4], s[6]) for s in self.steps()], [(80, 79, 0), (79, 78, 0), (78, 77, 0), (77, 76, 1)])
        with self.db() as db:
            self.assertEqual(db.execute("select wakeSupport from mice").fetchone()[0], 1)

    def testAMultiPointStepIsMinutesPerPoint(self):
        self.drops((0, 80), (1800, 79), (3600, 76))
        self.assertEqual(self.steps()[-1][2:5], (10.0, 79, 76))

    def testAClockThatSteppedBackSavesNoStepWithoutABootClock(self):
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.drops((5000, 80), (6800, 79), (100, 78))
        # stopping writes the held row, so the 79 to 78 step reaches the length check
        self.recorder.stop()
        self.assertIn((100, 0, 78), [row[:3] for row in self.rows()])
        self.assertEqual([(s[3], s[4]) for s in self.steps()], [(80, 79)])


class ClockStepTests(RecorderCase):
    def testAHeldRowIsWrittenOnTimeAfterTheWallClockStepsBack(self):
        self.event(0, 50)
        self.clock.now = START + 3
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_EVENT)
        self.at(5)
        self.assertIsNotNone(self.recorder.entries[KEY].held)
        self.clock.now = START - 2 * 60 * 60
        self.clock.now += history.HOLD_SECONDS
        self.recorder.tick()
        self.assertEqual([row[2] for row in self.rows()], [50, 49])

    def testAReadingWaitingWhenTheWallClockStepsBackIsWrittenOnTime(self):
        self.assertTrue(self.recorder.available())
        self.clock.now = START + 2 * 60 * 60
        self.recorder.addReading(KEY, battery(60), history.ORIGIN_EVENT)
        self.clock.now = START
        self.recorder.addReading(KEY, battery(59), history.ORIGIN_EVENT)
        self.clock.now += history.WAKE_SET_SECONDS + 1
        self.recorder.tick()
        self.assertEqual((len(self.rows()), self.recorder.entries[KEY].pending), (1, []))

    def testAnOpenRetryWaitsOnlyItsGapAfterTheWallClockStepsBack(self):
        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        self.clock.now = START + 2 * 60 * 60
        with mock.patch.object(sqlite3, "connect", failing):
            self.assertFalse(self.recorder.available())
        self.clock.now = START
        self.assertFalse(self.recorder.available())
        self.clock.now += history.OPEN_RETRY_SECONDS
        self.assertTrue(self.recorder.available())

    def testAWallClockJumpForwardDoesntLengthenADrop(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 80)
        self.event(1800, 79)
        self.clock.jump(60 * 60)
        self.event(7200, 78)
        self.event(9000, 77)
        self.assertEqual([(s[2], s[3], s[4], s[6]) for s in self.steps()], [(30.0, 80, 79, 0), (30.0, 79, 78, 1), (30.0, 78, 77, 1)])

    def testAWallClockJumpForwardKeepsAChargesFirstTwoMinutesOut(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 68)
        self.event(600, 68, charging=1, power=1)
        self.event(612, 69, charging=1, power=1)
        self.clock.jump(60 * 60)
        for seconds, percent in ((4224, 70), (4236, 71), (4360, 72), (4400, 73)):
            self.event(seconds, percent, charging=1, power=1)
        rises = [(row[3], row[6]) for row in self.steps() if row[0] == history.CHARGE]
        self.assertEqual(rises, [(68, 0), (69, 0), (70, 0), (71, 0), (72, 1)])

    def testAWallClockJumpBackDoesntMoveADischargesStart(self):
        self.event(0, 80)
        self.event(1800, 79)
        # the wall clock lands 30 s after the discharge started, while half an hour has really gone by
        self.clock.jump(-1800)
        self.event(30, 78)
        self.assertEqual(self.sessions()[0][3], 80)

    def testAWallClockJumpBackBeforeADischargesStartDoesntMoveItWithoutABootClock(self):
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.event(0, 80)
        self.event(1800, 79)
        self.clock.jump(-2 * 60 * 60)
        self.event(-3600, 78)
        self.assertEqual(self.sessions()[0][3], 80)

    def testASuspendInsideAStepCountsTowardItsLength(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 80)
        self.event(1800, 79)
        # the monotonic clock stops for the hour the laptop sleeps, while the boot clock counts it
        self.clock.suspend(60 * 60)
        self.event(7200, 78)
        self.assertEqual(self.steps()[-1][2:5], (90.0, 79, 78))


class ClockFrameTests(RecorderCase):
    def restart(self, bootId=BOOT):
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=bootId)

    def kill(self):
        # a power loss or a SIGKILL ends the run with no stop, while every commit that returned is on the disk
        self.recorder.store.closeQuietly()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)

    def testARestartAfterTheClockStepsBackKeepsTheChargesNewerReadings(self):
        # the charge's first reading was saved while the clock was two hours ahead
        self.event(7200, 50, charging=1, power=1)
        self.clock.jump(-7200)
        self.event(600, 51, charging=1, power=2)
        self.event(1200, 52, charging=1, power=2)
        self.restart()
        self.event(1800, 52, charging=0, power=None)
        self.assertEqual(self.sessions()[0][:6], (history.CHARGE, 0, 1200, 50, 52, 2))

    def testTheLastChargeAfterTheClockStepsBackIsTheNewestOne(self):
        self.event(7200, 50, charging=1, power=1)
        self.event(7800, 50, charging=0, power=0)
        self.clock.jump(-7800)
        self.event(600, 50, charging=1, power=2)
        self.assertEqual(self.recorder.summary(KEY)["lastCharge"]["power"], 2)

    def testARestartAfterTheClockStepsBackComparesWithTheNewestReading(self):
        self.event(7200, 50)
        self.clock.jump(-7200)
        self.event(600, 40)
        self.restart()
        # five points up from the newest reading, across the restart's gap, is a charge done while nothing watched
        self.event(1200, 45)
        self.assertIn((history.CHARGE, 600, 1200, 40, 45), [s[:5] for s in self.sessions()])

    def testTheOpenSummaryAfterTheClockStepsBackKeepsItsSteps(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 80)
        self.event(1800, 79)
        self.clock.jump(-6 * 60 * 60)
        for index in range(5):
            self.event(3600 + 1800 * index - 6 * 60 * 60, 78 - index)
        opened = self.recorder.summary(KEY)["open"]
        self.assertEqual((opened["kind"], opened["startAt"], opened["count"], opened["median"]), (history.DISCHARGE, START - 6 * 60 * 60, 5, 30.0))

    def chargeAcrossAReboot(self):
        # a charge saved while the clock was three hours ahead, and the machine rebooted before the clock was set, so no boot clock spans the two
        self.recorder.setWakeSupport(KEY, True)
        ahead = 3 * 60 * 60
        self.clock.jump(ahead)
        for index in range(4):
            self.event(ahead + 60 * index, 50 + index, charging=1, power=1)
        self.restart(bootId="next-boot")
        self.clock.jump(-ahead)
        for seconds, percent in ((240, 54), (300, 55), (360, 56)):
            self.event(seconds, percent, charging=1, power=1)

    def testARestoredChargeMeasuresItsFirstMinutesOnTheBootClock(self):
        self.chargeAcrossAReboot()
        # the step from 55 began four minutes into the charge, which the wall clock put three hours before its start
        self.assertEqual([(s[3], s[6]) for s in self.steps() if s[0] == history.CHARGE], [(54, 0), (55, 1), (50, 0), (51, 0), (52, 1)])

    def testAReadingSavedWhileTheClockWasAheadDoesntComeBackInTheChart(self):
        self.event(0, 80)
        self.clock.jump(2 * 60 * 60)
        self.event(7300, 79)
        self.clock.jump(-2 * 60 * 60)
        self.event(300, 78)
        self.at(8000)
        self.assertEqual(self.recorder.summary(KEY)["recent"][-1][1], 78)

    def testAReadingSavedWhileTheClockWasAheadDoesntComeBackAfterAKill(self):
        self.event(0, 80)
        self.clock.jump(2 * 60 * 60)
        self.event(7300, 79)
        self.kill()
        # the clock was set right before the next start
        self.clock.jump(-2 * 60 * 60)
        self.event(300, 78)
        self.at(8000)
        self.assertEqual(self.recorder.summary(KEY)["recent"][-1][1], 78)
        self.assertEqual([row[:3] for row in self.rows()][:2], [(0, history.ORIGIN_EVENT, 80), (100, history.ORIGIN_EVENT, 79)])

    def testALongSlotDoesntCarryAReadingSavedWhileTheClockWasAhead(self):
        ahead = 13 * 60 * 60
        self.clock.jump(ahead)
        self.event(ahead + 10, 80)
        self.clock.jump(-ahead)
        self.event(300, 78)
        self.clock.now = START + 3 * 24 * 60 * 60
        slots = self.recorder.levels(KEY, "month")["slots"]
        # the month's slots are twelve hours long and the 54th starts at START
        self.assertEqual([slot[1:3] for slot in slots[54:57]], [[78, 80], [78, 78], [78, 78]])

    def testASlowlySlewedClockMovesNothing(self):
        self.event(0, 80)
        self.clock.jump(-history.CLOCK_STEP_SECONDS)
        self.event(1800, 79)
        self.assertEqual([row[0] for row in self.rows()], [0, 1800])

    def testAClockStepMovesNoRowAnEarlierBootSaved(self):
        self.event(0, 80)
        self.restart(bootId="next-boot")
        ahead = 5 * 60 * 60
        self.clock.jump(ahead)
        self.event(ahead + 100, 79)
        self.clock.jump(-ahead)
        self.event(300, 78)
        self.assertEqual([row[:3] for row in self.rows() if row[1] != history.ORIGIN_GAP], [(0, history.ORIGIN_EVENT, 80), (100, history.ORIGIN_EVENT, 79), (300, history.ORIGIN_EVENT, 78)])

    def testAClockBehindTheHistoryMovesNoRowSavedBefore(self):
        # west of UTC, a quick reboot from Windows starts the clock hours behind the last run's rows, and time sync then sets it forward
        for seconds, percent in ((0, 80), (3600, 79), (7200, 78)):
            self.event(seconds, percent)
        saved = self.rows()
        self.restart(bootId="next-boot")
        behind = 5 * 60 * 60
        self.clock.jump(-behind)
        self.event(7300 - behind, 77)
        self.clock.jump(behind)
        self.event(7400, 76)
        self.assertEqual([row for row in self.rows() if 0 <= row[0] <= 7200], saved)
        self.assertEqual(self.rows()[-1][:3], (7400, history.ORIGIN_EVENT, 76))

    def testAPendingAndAHeldReadingFollowTheClock(self):
        self.event(0, 80)
        self.clock.now = START + 5
        self.recorder.addReading(KEY, battery(79), history.ORIGIN_EVENT)
        self.at(7)
        self.assertIsNotNone(self.recorder.entries[KEY].held)
        self.clock.jump(-3600)
        self.at(12 - 3600)
        self.clock.now = START + 100 - 3600
        self.recorder.addReading(KEY, battery(78), history.ORIGIN_EVENT)
        self.clock.jump(-3600)
        self.at(102 - 7200)
        self.assertEqual([row[:3] for row in self.rows()], [(-7200, 0, 80), (5 - 7200, 0, 79), (100 - 7200, 0, 78)])

    def testAClockStepAcrossARestartOnTheSameBootMovesTheLastRunsRows(self):
        self.event(0, 80)
        self.recorder.stop()
        # the clock was set back while the service was stopped
        self.clock.jump(-60 * 60)
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        self.event(600 - 60 * 60, 79)
        self.assertEqual([row[:3] for row in self.rows() if row[1] != history.ORIGIN_GAP], [(-3600, 0, 80), (600 - 3600, 0, 79)])

    def testAStepALockHidFromTheOpenMovesTheLastRunsRowsOnceTheLockIsGone(self):
        self.event(0, 80)
        self.recorder.stop()
        # the clock was set back while the service was stopped, and another process held the file as it started again
        self.clock.jump(-60 * 60)
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        with self.locked():
            self.assertTrue(self.recorder.available())
        self.event(600 - 60 * 60, 79)
        self.at(2 + history.OPEN_RETRY_SECONDS - 60 * 60)
        self.assertEqual([row[:3] for row in self.rows() if row[1] != history.ORIGIN_GAP], [(-3600, 0, 80), (600 - 3600, 0, 79)])

    def testAStepTheHistoryCouldntSaveMovesTheRowsAtTheNextLoad(self):
        self.event(0, 80)
        self.clock.jump(-60 * 60)
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.tick()
        self.event(600 - 60 * 60, 79)
        self.assertEqual([row[:3] for row in self.rows() if row[1] != history.ORIGIN_GAP], [(-3600, 0, 80), (600 - 3600, 0, 79)])

    def testAStepBeforeAMouseLoadsMovesTheRowsAnEarlierRunOnThisBootSaved(self):
        self.event(0, 80)
        self.restart()
        self.assertTrue(self.recorder.available())
        self.clock.jump(-60 * 60)
        self.event(600 - 60 * 60, 79)
        self.assertEqual([row[:3] for row in self.rows() if row[1] != history.ORIGIN_GAP], [(-3600, 0, 80), (600 - 3600, 0, 79)])

    def testNoQueryThatFollowsAStepScansATable(self):
        self.recorder.setWakeSupport(KEY, True)
        for seconds, percent in ((0, 80), (1800, 79), (3600, 78)):
            self.event(seconds, percent)
        self.event(3700, 78, charging=1, power=1)
        # a reboot, so loading looks up the newest date saved before this boot
        self.restart(bootId="next-boot")
        self.assertTrue(self.recorder.available())
        db = self.recorder.store.db
        statements = []
        db.set_trace_callback(statements.append)
        self.event(3800, 79, charging=1, power=1)
        self.event(3900, 80, charging=1, power=1)
        self.clock.jump(-2 * 60 * 60)
        self.event(4400 - 2 * 60 * 60, 81, charging=1, power=1)
        db.set_trace_callback(None)
        statements = [sql for sql in dict.fromkeys(statements) if sql.split()[0] in ("select", "insert", "update", "delete")]
        for fragment in ("update readings set at", "update steps set at", "update sessions set endAt", "update mice set firstSeen", "into boots", "from boots", "select max(at)"):
            self.assertTrue(any(fragment in sql for sql in statements), fragment)
        scans = [sql for sql in statements if any(row[3].startswith("SCAN") for row in db.execute("explain query plan " + sql))]
        self.assertEqual(scans, [])

    def dualBoot(self, ahead):
        # Windows keeps local time in the clock chip, so east of UTC Linux boots ahead until time sync sets it back a minute later
        self.clock.jump(ahead)
        self.recorder.setWakeSupport(KEY, True)
        self.event(ahead + 10, 50, charging=1, power=2)
        self.event(ahead + 40, 51, charging=1, power=2)
        self.clock.jump(-ahead)
        for index in range(5):
            self.event(100 + 60 * index, 52 + index, charging=1, power=2)
        self.restart()
        for index in range(8):
            self.event(600 + 60 * index, 57 + index, charging=1, power=2)
        self.at(1200)
        summary = self.recorder.summary(KEY)
        found = [self.rows(), self.steps(), summary["firstSeen"], summary["open"], summary["recent"], self.recorder.levels(KEY, "day")["slots"], self.recorder.levels(KEY, "month")["slots"]]
        self.event(1300, 64, charging=0, power=0)
        found += [self.sessions(), self.recorder.summary(KEY)["lastCharge"]]
        return found

    def testADualBootLeavesTheSameHistoryAsAClockThatWasRight(self):
        expected = self.dualBoot(0)
        self.recorder.stop()
        shutil.rmtree(self.folder)
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        self.assertEqual(self.dualBoot(5 * 60 * 60), expected)
        self.assertEqual(expected[-2][0][:6], (history.CHARGE, 10, 1020, 50, 64, 2))

    def testAHistoryWrittenAtTheLoopsStartOpensAndKeepsEveryRow(self):
        self.recorder.stop()
        path = os.path.join(self.folder, history.FILE_NAME)
        db = sqlite3.connect(path)
        with db:
            for statement in FIXTURE_SCHEMA:
                db.execute(statement)
            db.execute("pragma user_version = 1")
            db.execute("insert into mice (id, key, name, source, firstSeen, wakeSupport) values (1, ?, 'PRO X3 SUPERSTRIKE', 'hidpp', ?, 1)", (KEY, START))
            for index in range(3):
                db.execute("insert into readings (mouseId, at, seq, percent, charging, power, origin) values (1, ?, 0, ?, 0, 0, 0)", (START + 1800 * index, 80 - index))
            db.execute("insert into sessions (mouseId, kind, startAt, startPercent, readingCount, slowCount, errorCount, inferred) values (1, 0, ?, 80, 3, 0, 0, 0)", (START,))
            db.execute("insert into steps (mouseId, kind, at, seq, minutesPerPercent, fromPercent, toPercent, power, unbroken, session) values (1, 0, ?, 0, 30.0, 79, 78, 0, 1, 1)", (START + 3600,))
        db.close()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        saved = self.rows()
        self.clock.jump(4 * 60 * 60)
        for index in range(3):
            self.event(4 * 60 * 60 + 5400 + 1800 * index, 77 - index)
        self.clock.jump(-4 * 60 * 60)
        self.event(5400 + 1800 * 3, 74)
        self.assertEqual([name for name in os.listdir(self.folder) if ".broken-" in name], [])
        self.assertEqual(self.rows()[:len(saved)], saved)
        self.assertEqual([row[0] for row in self.rows() if row[1] == history.ORIGIN_EVENT], [0, 1800, 3600, 5400, 7200, 9000, 10800])
        self.assertEqual(self.sessions(), [(history.DISCHARGE, 0, None, 80, None, 0, 7, 0, 0, 0)])
        with self.db() as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 1)
            tables = {name: sql.lower() for name, sql in db.execute("select name, sql from sqlite_master where name not like 'sqlite_%'")}
        # every table and index fac27f9 made is left as it was, so that release still reads the file
        self.assertEqual([tables[name] for name in ("mice", "readings", "steps", "sessions", "sessionsByMouse")], [statement.lower() for statement in FIXTURE_SCHEMA])
        # the saved step, and the two whole drops after the gap the restart left
        self.assertEqual(self.recorder.summary(KEY)["open"]["count"], 3)


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

    def testADropRightAfterAWakeDoesntWhenTheWallClockJumpsBetween(self):
        self.event(0, 60)
        self.marker(1000, history.ORIGIN_WAKE)
        self.clock.jump(60 * 60)
        self.event(4610, 59)
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

    def savedFlags(self):
        with self.db() as db:
            return db.execute("select reportsItselfSince, wakeSupport from mice").fetchone()

    def testAClearThatFailsToSaveIsSavedByTheNextWrite(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.clock.now = START + 5000
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.addReading(KEY, battery(57), history.ORIGIN_READ, at=START + 5000)
        self.assertFalse(self.recorder.reportsItself(KEY))
        self.at(5000 + history.WAKE_SET_SECONDS)
        self.assertEqual(self.savedFlags(), (None, 1))

    def testAWakeSupportThatFailsToSaveIsSavedByTheNextWrite(self):
        self.event(0, 60)
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.setWakeSupport(KEY, False)
        self.assertFalse(self.recorder.entries[KEY].track.wakeSupport)
        self.event(1000, 59)
        self.assertEqual(self.savedFlags(), (None, 0))

    def testAWakeSupportSetWhileTheHistoryIsntOpenIsSavedWhenItOpens(self):
        self.event(0, 60)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.clock.now = START + 100
        with self.failOpening():
            self.recorder.setWakeSupport(KEY, False)
        self.clock.now = START + 100 + history.OPEN_RETRY_SECONDS
        self.event(800, 59)
        self.assertEqual((self.recorder.entries[KEY].track.wakeSupport, self.savedFlags()[1]), (False, 0))

    def testAClearWhileTheHistoryIsntOpenIsSavedWhenItOpens(self):
        self.event(0, 60)
        self.event(1000, 59)
        self.clock.now = START + 5000
        with self.failOpening():
            self.recorder.addReading(KEY, battery(57), history.ORIGIN_READ, at=START + 5000)
            self.at(5000 + history.WAKE_SET_SECONDS)
        self.assertFalse(self.recorder.reportsItself(KEY))
        self.clock.now = START + 5000 + history.OPEN_RETRY_SECONDS
        self.event(5700, 56)
        self.assertEqual((self.recorder.reportsItself(KEY), self.savedFlags()[0]), (False, None))

    def testAFlagSetWhileTheHistoryIsDownIsSavedForAMouseAlreadySaved(self):
        self.event(0, 60)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.clock.now = START + 100
        with self.failOpening():
            self.recorder.setWakeSupport(KEY, True)
            self.event(100, 60)
            self.event(1100, 59)
        self.assertTrue(self.recorder.reportsItself(KEY))
        self.event(2000, 58, origin=history.ORIGIN_READ)
        self.assertEqual((self.recorder.reportsItself(KEY), self.savedFlags()[0]), (True, START + 1100))

    def testAFlagSetBeforeTheMouseIsSavedIsSavedWithIt(self):
        key = "aabbccdd"
        self.clock.now = START
        with self.failOpening():
            self.recorder.setWakeSupport(key, True)
            self.event(0, 60, key=key)
            self.event(1000, 59, key=key)
        self.assertTrue(self.recorder.reportsItself(key))
        self.event(1700, 58, key=key, origin=history.ORIGIN_READ)
        with self.db() as db:
            self.assertEqual(db.execute("select reportsItselfSince, wakeSupport from mice where key = ?", (key,)).fetchone(), (START + 1000, 1))

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
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
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
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual([row[2] for row in self.rows()], [50, None])

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
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.recorder.addReading(KEY, battery(48), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual([row[2] for row in self.rows()], [49, None])

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

    def testAWriteThatFailsPartwayLeavesNothingOfItBehind(self):
        self.event(0, 50)
        # the reading is inserted first, so failing the session update after it shows the rollback took the reading back out
        self.failQuery(sqlite3.OperationalError("database or disk is full"), "update sessions")
        self.event(1800, 49)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50)])
        self.event(3600, 48)
        self.assertEqual([row[:3] for row in self.rows()], [(0, 0, 50), (3600, 4, None), (3600, 0, 48)])
        self.assertEqual([session[6] for session in self.sessions()], [2])

    def testASaveFailureThatComesBackAfterARecoveryIsPrintedAgain(self):
        self.event(0, 50)
        for seconds, percent in ((1800, 49), (5400, 47)):
            with self.failOnce(sqlite3.OperationalError("database or disk is full")):
                self.event(seconds, percent)
            self.event(seconds + 1800, percent - 1)
        self.assertEqual(self.errors.getvalue().splitlines(), ["Couldn't save to the history: database or disk is full", "The history is saving again"] * 2)

    def testForgettingAMouseSavesWhatItHeldAndDropsItsEntry(self):
        self.event(0, 50)
        self.event(5, 49)
        self.recorder.forget(KEY)
        self.assertEqual([row[2] for row in self.rows()], [50, 49])
        self.assertNotIn(KEY, self.recorder.entries)

    def testAForgottenMouseComesBackWithItsSavedFlags(self):
        self.recorder.setWakeSupport(KEY, True)
        self.recorder.forget(KEY)
        self.event(600, 50)
        self.assertTrue(self.recorder.entries[KEY].track.wakeSupport)

    def testAForgottenPathKeyStartsOverWithoutReportsItself(self):
        key = "/dev/hidraw4#1"
        self.recorder.entryFor(key, pathKey=True).track.reportsItselfSince = START
        self.recorder.forget(key)
        self.assertFalse(self.recorder.reportsItself(key))

    def testAForgottenPathKeyWithItsWakeSupportIsDroppedToo(self):
        # a path key's flags never reach the database, so they can't hold its entry back
        key = "/dev/hidraw4#1"
        self.recorder.setWakeSupport(key, True, pathKey=True)
        self.recorder.forget(key)
        self.assertNotIn(key, self.recorder.entries)

    def testAForgottenKeyIsDroppedWhenTheHistoryNeverOpens(self):
        recorder = history.Recorder(None, self.clock, timers=False, monotonic=self.clock.monotonic)
        recorder.setWakeSupport(KEY, True)
        recorder.forget(KEY)
        self.assertNotIn(KEY, recorder.entries)

    def testARevisionNeverComesBackAfterAForget(self):
        self.event(0, 50)
        self.marker(600, history.ORIGIN_WAKE)
        before = self.recorder.revision(KEY)
        self.recorder.forget(KEY)
        self.event(1200, 49)
        self.assertGreater(self.recorder.revision(KEY), before)

    def testUPowerCantForgetAMouseReadOverHidpp(self):
        self.event(0, 50)
        self.recorder.forget(KEY, source=history.SOURCE_UPOWER)
        self.assertIn(KEY, self.recorder.entries)

    def miceRows(self):
        with self.db() as db:
            return [key for (key,) in db.execute("select key from mice order by id")]

    def testOnlyTheBudgetOfNewMiceGetRowsAtOnce(self):
        for index in range(history.NEW_MICE_BURST + 5):
            self.recorder.setWakeSupport("%08x" % index, True)
        self.assertEqual(len(self.miceRows()), history.NEW_MICE_BURST)

    def testAMouseLeftOutIsSavedOnceTheBudgetRefills(self):
        for index in range(history.NEW_MICE_BURST):
            self.recorder.setWakeSupport("%08x" % index, True)
        self.event(0, 50, key="ffffffff")
        self.assertNotIn("ffffffff", self.miceRows())
        self.event(history.NEW_MICE_REFILL_SECONDS + 10, 49, key="ffffffff")
        self.assertIn("ffffffff", self.miceRows())
        with self.db() as db:
            rows = db.execute("select r.origin, r.percent from readings r join mice m on m.id = r.mouseId where m.key = 'ffffffff' order by r.at, r.seq").fetchall()
        self.assertEqual(rows, [(history.ORIGIN_GAP, None), (history.ORIGIN_EVENT, 49)])

    def testAMouseWithARowAlreadyCostsNothing(self):
        for index in range(history.NEW_MICE_BURST):
            self.recorder.setWakeSupport("%08x" % index, True)
        self.recorder.forget("%08x" % 0)
        self.event(0, 50, key="%08x" % 0)
        with self.db() as db:
            self.assertEqual(db.execute("select count(*) from readings where origin = 0").fetchone()[0], 1)

    def testNewMiceLeftOutSayItOnceAndAgainWhenTheyreSaved(self):
        for index in range(history.NEW_MICE_BURST + 3):
            self.recorder.setWakeSupport("%08x" % index, True)
        self.event(history.NEW_MICE_REFILL_SECONDS + 10, 49, key="%08x" % (history.NEW_MICE_BURST + 2))
        self.assertEqual(self.errors.getvalue().splitlines(), ["More new mice showed up than the history adds at once, so some aren't saved yet", "New mice are saved to the history again"])

    def testAMouseLeftOutIsForgottenWithItsFlags(self):
        for index in range(history.NEW_MICE_BURST + 1):
            self.recorder.setWakeSupport("%08x" % index, True)
        key = "%08x" % history.NEW_MICE_BURST
        self.recorder.forget(key)
        self.assertNotIn(key, self.recorder.entries)

    def testAChargesFirstTwoMinutesGiveNoWholeSteps(self):
        # the X3's first cable charge on 2026-10-09 rose a point every few seconds for about a minute before it settled
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 68)
        for seconds, percent in ((600, 68), (612, 69), (624, 70), (636, 71), (648, 72), (760, 73), (800, 74), (840, 75)):
            self.event(seconds, percent, charging=1, power=1)
        rises = [(row[3], row[6]) for row in self.steps() if row[0] == history.CHARGE]
        self.assertEqual(rises, [(68, 0), (69, 0), (70, 0), (71, 0), (72, 0), (73, 1), (74, 1)])

    def testADischargesStartFollowsAFallInItsFirstMinute(self):
        # on 2026-10-09 the X3 read 100% full as it went from the cable onto the pad, then counted down to its real level
        self.event(0, 85, charging=1, power=1)
        self.event(600, 100, charging=3, power=2)
        for seconds, percent in ((601, 99), (612, 90), (625, 84), (700, 83)):
            self.event(seconds, percent)
        self.event(4000, 60, charging=1, power=2)
        discharges = [row for row in self.sessions() if row[0] == history.DISCHARGE]
        self.assertEqual(discharges[0][3], 84)

    def testADischargesFirstMinuteGivesNoWholeSteps(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 85, charging=1, power=1)
        self.event(600, 100, charging=3, power=2)
        for seconds, percent in ((601, 99), (612, 90), (625, 84), (700, 83), (2500, 82)):
            self.event(seconds, percent)
        drops = [(row[3], row[4], row[6]) for row in self.steps() if row[0] == history.DISCHARGE]
        self.assertEqual(drops, [(100, 99, 0), (99, 90, 0), (90, 84, 0), (84, 83, 0), (83, 82, 1)])
        self.assertEqual((self.recorder.summary(KEY)["open"]["median"], self.recorder.summary(KEY)["open"]["count"]), (30.0, 1))

    def testARealFullStartsItsDischargeAtFull(self):
        self.event(0, 95, charging=1, power=1)
        self.event(600, 100, charging=3, power=1)
        self.event(700, 100)
        self.event(2000, 99)
        self.event(5000, 60, charging=1, power=1)
        discharges = [row for row in self.sessions() if row[0] == history.DISCHARGE]
        self.assertEqual(discharges[0][3], 100)

    def flapCharging(self, seconds, count):
        # every reading comes in at the same moment, as a device flooding the node would send them
        self.clock.now = START + seconds
        for i in range(count):
            self.recorder.addReading(KEY, battery(50, (i + 1) % 2), history.ORIGIN_EVENT, at=START + seconds)
        self.at(seconds + history.WAKE_SET_SECONDS)

    def testAFlappingChargingCodeSavesNoMoreThanTheBudget(self):
        self.flapCharging(0, 100)
        self.assertEqual(len(self.rows()), history.WRITE_BURST)

    def testAFloodOfWakesSavesNoMoreThanTheBudget(self):
        self.event(0, 50)
        for i in range(100):
            self.marker(10, history.ORIGIN_WAKE)
        self.assertEqual(len(self.rows()), history.WRITE_BURST)

    def testASlowFloodSavesOneRowAndItsGapPerRefill(self):
        self.flapCharging(0, 100)
        for i in range(1, 11):
            self.flapCharging(i * history.WRITE_REFILL_SECONDS, 10)
        rows = self.rows()[history.WRITE_BURST:]
        self.assertEqual([row[1] for row in rows], [history.ORIGIN_GAP, history.ORIGIN_EVENT] * 10)

    def testRowsLeftOutByTheBudgetAreFollowedByAGapOnceItRefills(self):
        self.flapCharging(0, 100)
        self.event(3600, 49)
        self.assertEqual(self.rows()[-2:], [(3600, history.ORIGIN_GAP, None, None, None), (3600, history.ORIGIN_EVENT, 49, 0, 0)])

    def testTheBudgetSaysOnceWhenItLeavesRowsOutAndAgainOnceItsFull(self):
        self.flapCharging(0, 100)
        for i in range(1, 11):
            self.flapCharging(i * history.WRITE_REFILL_SECONDS, 10)
        self.event(3600, 49)
        lines = self.errors.getvalue().splitlines()
        self.assertEqual(lines, ["Mouse " + KEY + " sent more changes than the history saves, so some were left out", "Mouse " + KEY + "'s changes are saved again"])

    def testAFullChargeAtOneStepEveryTwentySecondsKeepsEveryRow(self):
        self.recorder.setWakeSupport(KEY, True)
        for i in range(81):
            self.event(i * 20, 20 + i, charging=1)
        self.event(81 * 20, 100, charging=3)
        rows = self.rows()
        self.assertEqual(len(rows), 82)
        self.assertNotIn(history.ORIGIN_GAP, [row[1] for row in rows])

    def testARestartReloadsTheOpenSession(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60, charging=1, power=2)
        self.event(600, 61, charging=1, power=2)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        # a report without the power byte, so only the reloaded charge's charging code can end it
        self.event(1200, 61, charging=0, power=None)
        self.assertEqual([(s[0], s[1], s[2]) for s in self.sessions()], [(1, 0, 600), (0, 1200, None)])
        self.assertEqual([row[1] for row in self.rows()][-2:], [4, 0])

    def testARestartReloadsTheLastPower(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60, charging=1, power=2)
        self.event(600, 61, charging=1, power=2)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        # the code still says charging, so only the reloaded power going to 0 can end the charge
        self.event(1200, 61, charging=1, power=0)
        self.assertEqual([(s[0], s[1], s[2]) for s in self.sessions()], [(1, 0, 600), (0, 1200, None)])

    def chargeWithPowers(self, powers, restartAfter=None):
        self.recorder.setWakeSupport(KEY, True)
        for index, power in enumerate(powers):
            self.event(index * 600, 50 + index, charging=1, power=power)
            if (index == restartAfter):
                self.recorder.stop()
                self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        return self.sessions()[0][5]

    def testARestartKeepsEachPowersOwnCount(self):
        # 1, 1, 2 was saved as 1 and reloaded as three readings of it, which outvoted the two of 2 that followed
        self.assertEqual(self.chargeWithPowers((1, 1, 2, 2, 2), restartAfter=2), 2)

    def testARestartKeepsATieWithTheFirstPowerSeen(self):
        # the restart comes before the power was saved twice, so the tie has to come back in the order it was seen
        self.assertEqual(self.chargeWithPowers((2, 1, 1, 2), restartAfter=1), 2)

    def testARestartKeepsTheLowestOfTheSessionsOwnReadings(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 40, charging=1, power=2)
        # the charge's last reading and the full that ends it come in the same second
        self.clock.now = START + 600
        self.recorder.addReading(KEY, battery(41, charging=1, power=2), history.ORIGIN_EVENT, at=START + 600)
        self.recorder.addReading(KEY, battery(100, charging=3, power=0), history.ORIGIN_EVENT, at=START + 600)
        self.at(602)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        for seconds, percent in ((2400, 99), (4200, 98), (6000, 97)):
            self.event(seconds, percent)
        self.assertEqual([(s[3], s[4], s[6]) for s in self.steps() if s[0] == history.DISCHARGE], [(99, 98, 0), (98, 97, 1)])

    def testASummaryAfterARestartKeepsTheSavedFlagsOfAMouseNotLoadedYet(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60)
        self.event(1000, 59)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        # UPower names the key before the HID++ search does, which makes an entry without loading it
        self.recorder.addReading(KEY, battery(58), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 1200)
        summary = self.recorder.summary(KEY)
        self.assertEqual((summary["wakeSupport"], summary["reportsItself"]), (True, True))

    def testASummaryShowsAFlagChangeTheHistoryHasntSavedYet(self):
        self.recorder.setWakeSupport(KEY, True)
        self.event(0, 60)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.clock.now = START + 100
        with self.failOpening():
            self.recorder.setWakeSupport(KEY, False)
        self.clock.now = START + 100 + history.OPEN_RETRY_SECONDS
        self.assertFalse(self.recorder.entries[KEY].loaded)
        self.assertFalse(self.recorder.summary(KEY)["wakeSupport"])

    def testAFailedHidppLookupForAUPowerReadingDropsItAndAsksAgain(self):
        real = self.failQuery(sqlite3.OperationalError("disk I/O error"))
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        entry = self.recorder.entries["9a1b2c3d"]
        self.assertEqual((entry.source, entry.pending), (None, []))
        self.assertIn("Couldn't read the history: disk I/O error", self.errors.getvalue())
        self.recorder.store.db = real
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 60)
        self.assertEqual((entry.source, len(entry.pending)), (history.SOURCE_UPOWER, 1))

    def testAMouseWhoseReadingsDontChangeIsSavedOnceAFailedOpenIsRetried(self):
        self.assertTrue(self.recorder.available())
        with self.failOpening():
            self.event(0, 80)
        for seconds in range(300, 3601, 300):
            self.event(seconds, 80)
        self.assertEqual([row[:3] for row in self.rows()], [(0, history.ORIGIN_EVENT, 80)])

    def testASavedMouseWhoseReadingsDontChangeIsSavedAfterItsGapOnceAFailedOpenIsRetried(self):
        self.event(0, 70)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        with self.failOpening():
            self.event(1000, 80)
        for seconds in range(1300, 4601, 300):
            self.event(seconds, 80)
        self.assertEqual([row[:3] for row in self.rows()], [(0, history.ORIGIN_EVENT, 70), (2, history.ORIGIN_GAP, None), (1000, history.ORIGIN_GAP, None), (1000, history.ORIGIN_EVENT, 80)])

    def testAWriteThatFailedIsSavedOnTheRetryWaitWhenNoOtherReadingComes(self):
        self.event(0, 80)
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.event(1800, 79)
        self.at(1802 + history.OPEN_RETRY_SECONDS)
        self.assertEqual([row[:3] for row in self.rows()], [(0, history.ORIGIN_EVENT, 80), (1800, history.ORIGIN_GAP, None), (1800, history.ORIGIN_EVENT, 79)])

    def testAFlagThatFailedToSaveIsSavedOnTheRetryWaitAfterItsMouseLeaves(self):
        self.event(0, 80)
        with self.failOnce(sqlite3.OperationalError("database or disk is full")):
            self.recorder.setWakeSupport(KEY, True)
        self.recorder.forget(KEY)
        self.at(2 + history.OPEN_RETRY_SECONDS)
        with self.db() as db:
            self.assertEqual(db.execute("select wakeSupport from mice").fetchone()[0], 1)

    def testAHistoryThatCantOpenYetLeavesASavedHidppKeyToHidpp(self):
        self.event(0, 50)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)

        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        self.clock.now = START + 600
        with mock.patch.object(sqlite3, "connect", failing):
            self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        entry = self.recorder.entries[KEY]
        self.assertEqual((entry.source, entry.pending), (None, []))
        self.clock.now = START + 600 + history.OPEN_RETRY_SECONDS
        self.recorder.addReading(KEY, battery(48), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 1200)
        self.at(1300)
        self.assertEqual(([row[2] for row in self.rows()], entry.source), ([50, None], history.SOURCE_HIDPP))

    def testAHistoryThatNeverOpensLetsUPowerHaveTheKey(self):
        self.recorder.store.tooNew = True
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.assertEqual(self.recorder.entries["9a1b2c3d"].source, history.SOURCE_UPOWER)

    def testAUPowerRowWaitingWhenLoadFindsAHidppKeyIsLeftOut(self):
        self.event(0, 50)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        with mock.patch.object(self.recorder, "isHidppKey", return_value=False):
            self.recorder.addReading(KEY, battery(49), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START + 600)
        self.at(700)
        self.assertEqual(([row[2] for row in self.rows()], self.recorder.entries[KEY].source), ([50, None], history.SOURCE_HIDPP))

    def testACorruptHidppLookupForAUPowerReadingMarksTheHistoryCorrupt(self):
        self.failQuery(corruption())
        self.recorder.addReading("9a1b2c3d", battery(70), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.assertTrue(self.recorder.store.brokenThisRun)
        self.assertFalse(self.recorder.available())

    def testAReloadedVoltageOnlySessionEndsAtItsLastReading(self):
        self.event(0, None, power=None, millivolts=3900)
        self.event(400, None, power=None, millivolts=3850)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic)
        self.event(1200, None, charging=1, power=None, millivolts=4000)
        self.assertEqual([(s[0], s[1], s[2]) for s in self.sessions()], [(0, 0, 400), (1, 1200, None)])

    def testWithNoFolderTheRulesStillRunInMemory(self):
        recorder = history.Recorder(None, self.clock, timers=False, monotonic=self.clock.monotonic)
        recorder.setWakeSupport(KEY, True)
        recorder.addReading(KEY, battery(60), history.ORIGIN_EVENT, at=START)
        recorder.addReading(KEY, battery(59), history.ORIGIN_EVENT, at=START + 1000)
        self.clock.now = START + 1100
        recorder.tick()
        self.assertTrue(recorder.reportsItself(KEY))
        self.assertFalse(recorder.available())
        recorder.stop()



class RecoveryTests(RecorderCase):
    def kill(self):
        # a power loss or a SIGKILL ends the run with no stop, so nothing more is written, while every commit that returned is on the disk
        self.recorder.store.closeQuietly()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)

    def dayLevels(self):
        return [slot[1] for slot in self.recorder.levels(KEY, "day")["slots"]]

    def heartbeat(self):
        with self.db() as db:
            return db.execute("select at - ? from heartbeat", (START,)).fetchone()[0]

    def gaps(self):
        return [row[0] for row in self.rows() if row[1] == history.ORIGIN_GAP]

    def testAKilledRunLeavesAGapWhereItWasLastSeen(self):
        self.event(0, 80)
        self.at(5 * 60 + 10)
        self.kill()
        self.clock.now = START + 24 * 60 * 60
        self.assertTrue(self.recorder.available())
        # the day ends now and starts at the reading, so nothing is known from the heartbeat on
        self.assertEqual(set(self.dayLevels()), {None})
        self.assertEqual(self.gaps(), [5 * 60 + 10])

    def testAKilledRunGapsAfterAReadingNewerThanItsHeartbeat(self):
        self.event(0, 80)
        self.event(100, 79)
        self.kill()
        self.clock.now = START + 5000
        self.assertTrue(self.recorder.available())
        self.assertEqual(self.gaps(), [100])

    def testAHeartbeatFromAClockThatWasAheadPutsTheGapNoLaterThanNow(self):
        self.event(0, 80)
        self.clock.jump(2 * 60 * 60)
        self.at(2 * 60 * 60 + 400)
        self.assertEqual(self.heartbeat(), 2 * 60 * 60 + 400)
        self.kill()
        # the clock was set right before the next start
        self.clock.jump(-2 * 60 * 60)
        self.at(450)
        self.assertTrue(self.recorder.available())
        self.event(500, 79)
        self.assertIn((450, history.ORIGIN_GAP, None, None, None), self.rows())
        self.assertEqual(self.rows()[-1], (500, history.ORIGIN_EVENT, 79, 0, 0))

    def testAMouseWhoseLastRowIsAGapGetsNoOther(self):
        self.event(0, 80)
        self.marker(100, history.ORIGIN_GAP)
        self.at(1000)
        self.kill()
        self.clock.now = START + 5000
        self.assertTrue(self.recorder.available())
        self.assertEqual(self.gaps(), [100])

    def testTwoKillsInARowAddOneGap(self):
        self.event(0, 80)
        self.kill()
        self.clock.now = START + 5000
        self.assertTrue(self.recorder.available())
        self.kill()
        self.clock.now = START + 9000
        self.assertTrue(self.recorder.available())
        self.assertEqual(len(self.gaps()), 1)

    def testAHistoryFromBeforeTheHeartbeatOpensWithTodaysGap(self):
        self.event(0, 80)
        self.recorder.stop()
        with self.db() as db:
            db.execute("drop table heartbeat")
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        self.clock.now = START + 5000
        self.assertTrue(self.recorder.available())
        self.assertEqual(self.gaps(), [])
        with self.db() as db:
            self.assertEqual(db.execute("pragma user_version").fetchone()[0], 1)
        self.assertEqual(self.heartbeat(), 5000)
        self.event(6000, 79)
        self.assertEqual(self.rows()[-2:], [(6000, history.ORIGIN_GAP, None, None, None), (6000, history.ORIGIN_EVENT, 79, 0, 0)])

    def testTheHeartbeatIsSavedAtMostEveryFiveMinutes(self):
        self.event(0, 80)
        opened = self.heartbeat()
        self.at(5 * 60)
        self.assertEqual(self.heartbeat(), opened)
        self.at(opened + 5 * 60)
        self.assertEqual(self.heartbeat(), opened + 5 * 60)
        self.at(opened + 5 * 60 + 100)
        self.assertEqual(self.heartbeat(), opened + 5 * 60)

    def testNoHeartbeatIsSavedWithoutAMouse(self):
        self.assertTrue(self.recorder.available())
        self.at(60 * 60)
        self.recorder.stop()
        self.assertEqual(self.heartbeat(), 0)

    def testAHeartbeatThatCantBeSavedLeavesTheReadingsAlone(self):
        self.event(0, 80)
        self.failQuery(sqlite3.OperationalError("database or disk is full"), "into heartbeat")
        self.at(400)
        self.assertIn("Couldn't save to the history: database or disk is full", self.errors.getvalue())
        self.event(500, 79)
        self.assertEqual(self.rows()[-1], (500, history.ORIGIN_EVENT, 79, 0, 0))

    def testAHistoryThatCantSaveAsItOpensStillOpensForReading(self):
        self.event(0, 80)
        self.kill()
        real = sqlite3.connect

        class FailingDb:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if ("heartbeat" in sql):
                    raise sqlite3.OperationalError("attempt to write a readonly database")
                return self.db.execute(sql, *args)

            def close(self):
                self.db.close()
        with mock.patch.object(sqlite3, "connect", lambda *args, **kwargs: FailingDb(real(*args, **kwargs))):
            self.assertTrue(self.recorder.available())
        self.assertIn("Couldn't save to the history: attempt to write a readonly database", self.errors.getvalue())
        self.assertEqual(self.recorder.summary(KEY)["name"], "PRO X3 SUPERSTRIKE")

    def testARecoveryALockHeldUpGoesInOnceTheLockIsGone(self):
        # another process held the file through the open a day later and then let go, while a second mouse kept the run watched
        self.event(0, 80)
        self.at(5 * 60 + 10)
        self.kill()
        day = 24 * 60 * 60
        self.clock.now = START + day
        with self.locked():
            self.assertTrue(self.recorder.available())
        self.event(day + 100, 60, key=OTHER)
        for seconds in range(day + 300, day + 3600, 300):
            self.at(seconds)
        self.assertEqual(self.gaps(), [5 * 60 + 10])
        self.assertEqual(set(self.dayLevels()), {None})
        self.assertEqual(self.heartbeat(), seconds)
        self.assertIn((day + 100, history.ORIGIN_EVENT, 60, 0, 0), self.rows())

    def testAMouseThatComesBackWhileTheRecoveryWaitsKeepsItsLevelAfterTheGap(self):
        self.event(0, 80)
        self.at(5 * 60 + 10)
        self.kill()
        self.clock.now = START + 5000
        with self.locked():
            self.assertTrue(self.recorder.available())
        self.event(5100, 70)
        self.at(5000 + history.OPEN_RETRY_SECONDS)
        # a row saved before the recovery's gap would have the gap put after it, which would hide the level the mouse came back at
        self.assertEqual([row[:3] for row in self.rows()], [(0, history.ORIGIN_EVENT, 80), (310, history.ORIGIN_GAP, None), (5100, history.ORIGIN_GAP, None), (5100, history.ORIGIN_EVENT, 70)])
        self.assertEqual(self.recorder.summary(KEY)["recent"][-1][1], 70)

    def testAHeartbeatTableALockHeldUpIsMadeOnceTheLockIsGone(self):
        self.event(0, 80)
        self.recorder.stop()
        with self.db() as db:
            db.execute("drop table heartbeat")
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        self.clock.now = START + 5000
        with self.locked():
            self.assertTrue(self.recorder.available())
        self.event(5100, 79)
        for seconds in range(5300, 5000 + 4 * 3600, 300):
            self.at(seconds)
        self.assertEqual(self.heartbeat(), seconds)
        self.assertEqual(self.rows()[-2:], [(5100, history.ORIGIN_GAP, None, None, None), (5100, history.ORIGIN_EVENT, 79, 0, 0)])
        self.assertEqual(self.errors.getvalue().splitlines(), ["Couldn't save to the history: SQLITE_BUSY", "The history is saving again"])

    def testCorruptionFoundRetryingTheRecoveryClosesTheHistory(self):
        self.event(0, 80)
        self.kill()
        real = sqlite3.connect
        errors = [sqlite3.OperationalError("database is locked"), corruption()]

        class FailingDb:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if ("heartbeat" in sql and errors):
                    raise errors.pop(0)
                return self.db.execute(sql, *args)

            def close(self):
                self.db.close()
        with mock.patch.object(sqlite3, "connect", lambda *args, **kwargs: FailingDb(real(*args, **kwargs))):
            self.clock.now = START + 100
            self.assertTrue(self.recorder.available())
            self.at(100 + history.OPEN_RETRY_SECONDS)
        self.assertFalse(self.recorder.store.isOpen())
        self.assertTrue(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        self.assertIn("The history is corrupt", self.errors.getvalue())

    def testASigkilledRecorderKeepsItsCommitsAndGetsItsGap(self):
        # a real recorder in its own process saves three readings, starts a fourth transaction, and is killed before it commits, so SQLite never closes the file
        script = "\n".join((
            "import os, signal, sys, time",
            "sys.path.insert(0, sys.argv[2])",
            "import history, hidpp",
            "recorder = history.Recorder(sys.argv[1], timers=False)",
            "start = time.monotonic() - 100",
            "for index, percent in enumerate((80, 79, 78)):",
            "    recorder.addReading(sys.argv[3], hidpp.BatteryReading(percent, 0, None, None, 0), history.ORIGIN_EVENT, name='PRO X3 SUPERSTRIKE', at=time.time() - 100 + 20 * index, arrived=start + 20 * index)",
            "    recorder.tick()",
            "db = recorder.store.db",
            "db.execute('begin immediate')",
            "db.execute('insert into readings (mouseId, at, seq, percent, charging, power, origin) values (1, ?, 0, 11, 0, 0, 0)', (int(time.time()),))",
            "os.kill(os.getpid(), signal.SIGKILL)",
        ))
        source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        started = int(time.time())
        result = subprocess.run([sys.executable, "-c", script, self.folder, source, KEY], capture_output=True, timeout=60, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        path = os.path.join(self.folder, history.FILE_NAME)
        self.assertGreater(os.path.getsize(path + "-wal"), 0)
        recorder = history.Recorder(self.folder, timers=False)
        try:
            self.assertTrue(recorder.available())
            rows = recorder.store.db.execute("select at, origin, percent from readings order by at, seq").fetchall()
        finally:
            recorder.stop()
        self.assertEqual([row[1:] for row in rows], [(history.ORIGIN_EVENT, 80), (history.ORIGIN_EVENT, 79), (history.ORIGIN_EVENT, 78), (history.ORIGIN_GAP, None)])
        # the gap goes where the killed run was last seen, which is its open, since that came after its readings
        self.assertGreaterEqual(rows[-1][0], started)
        self.assertLessEqual(rows[-1][0], int(time.time()))
        self.assertEqual([name for name in os.listdir(self.folder) if ".broken-" in name], [])
        self.assertNotIn("corrupt", self.errors.getvalue())

    def testACleanStopLeavesAUPowerMouseAGapAtTheStop(self):
        # the UPower history writes no gap row when the service stops, so the next start puts one where the last run stopped
        self.recorder.addReading(KEY, battery(80), history.ORIGIN_UPOWER, source=history.SOURCE_UPOWER, at=START)
        self.at(2)
        self.at(1000)
        self.recorder.stop()
        self.recorder = history.Recorder(self.folder, self.clock, timers=False, monotonic=self.clock.monotonic, boottime=self.clock.boottime, bootId=BOOT)
        self.clock.now = START + 9000
        self.assertTrue(self.recorder.available())
        self.assertEqual([row[:2] for row in self.rows()], [(0, history.ORIGIN_UPOWER), (1000, history.ORIGIN_GAP)])


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

    def testTheSummarySaysWhetherWakesAreVisible(self):
        self.assertTrue(self.recorder.summary(KEY)["wakesVisible"])
        self.recorder.setWakeSupport(KEY, None)
        summary = self.recorder.summary(KEY)
        # the saved flag stands while a lookup hasn't answered, but no wake can be seen until one does
        self.assertEqual((summary["wakeSupport"], summary["wakesVisible"]), (True, False))
        self.recorder.setWakeSupport(KEY, True)
        self.assertTrue(self.recorder.summary(KEY)["wakesVisible"])

    def testAnUnknownKeyIsAnEmptyHistory(self):
        summary = self.recorder.summary("ffffffff")
        self.assertEqual((summary["key"], summary["sessions"], summary["open"], summary["charge"]), ("ffffffff", [], None, []))

    def testAKeyFromAnyAccountIsOnlyEverAValue(self):
        # any local account may send any key, so it's bound as a parameter and never read as SQL
        for key in ("x' or '1'='1", "'; drop table readings; --", "k" * 100000, ""):
            self.assertEqual(self.recorder.summary(key), history.emptyHistory(key))
            self.assertEqual(self.recorder.levels(key, "year"), {"version": history.LEVELS_VERSION, "key": key, "range": "year", "slots": []})
        with self.recorder.store.transaction():
            self.assertEqual(self.database.execute("select count(*) from mice").fetchone()[0], 1)

    def testNoHistoryFolderMeansUnavailable(self):
        recorder = history.Recorder(None, self.clock, timers=False, monotonic=self.clock.monotonic)
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

    def testTheOpenSessionsStepsAreARangeFromItsStart(self):
        session = self.addSession(history.DISCHARGE, 5000)
        self.addStep(session, history.DISCHARGE, 5001, 30.0, 75, 74)
        statements = []
        self.database.set_trace_callback(statements.append)
        self.recorder.summary(KEY)
        self.database.set_trace_callback(None)
        found = [sql for sql in statements if "from steps" in sql and "session = " in sql]
        self.assertEqual(len(found), 1)
        plan = " ".join(row[3] for row in self.database.execute("explain query plan " + found[0]))
        self.assertIn("at>?", plan.replace(" ", ""))

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

    def testAReadingOrAChargeInTheQuerysOwnSecondIsInTheNewestSlot(self):
        now = 30 * 24 * 60 * 60
        self.addReadingRow(60, percent=90)
        self.addReadingRow(now, percent=70)
        self.addSession(history.CHARGE, now, None, 70, None)
        self.clock.now = START + now + 0.4
        newest = self.recorder.summary(KEY)["recent"][-1]
        self.assertEqual((newest[1], newest[4]), (70, True))
        for name in ("day", "week"):
            newest = self.recorder.levels(KEY, name)["slots"][-1]
            self.assertEqual((newest[1], newest[4]), (70, True), name)
        for name in history.SPAN_RANGES:
            newest = self.recorder.levels(KEY, name)["slots"][-1]
            self.assertEqual((newest[1], newest[3]), (70, 1), name)

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
            history.Recorder(None, self.clock, timers=False, monotonic=self.clock.monotonic).levels(KEY, "year")

if (__name__ == "__main__"):
    unittest.main()
