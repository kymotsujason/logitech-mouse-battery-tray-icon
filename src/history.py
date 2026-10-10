import contextlib
import copy
import json
import os
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field, replace

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import hidpp
from exceptionHooks import printLine

FILE_NAME = "history.sqlite3"
# corruption found during a run leaves this beside the database, so the next start runs the full check, since quick_check doesn't compare indexes with their tables
INTEGRITY_FLAG = "history.check-integrity"
SCHEMA_VERSION = 1
OPEN_RETRY_SECONDS = 600
# a history that turns corrupt this often in one run is left closed until the next start, so a failing disk can't fill the folder with broken copies
CORRUPT_REOPENS = 3
HOLD_SECONDS = 10
# the hold already keeps a change of percent alone to one row per 10 s, so this only bites on codes, power, and markers that flood in
WRITE_BURST = 30
WRITE_REFILL_SECONDS = HOLD_SECONDS
# measured on the X3 on 2026-10-09: one cable charge rose a point every few seconds for about a minute before it settled, and a full reading on going from the cable to the pad counted down to the real level within about 20 s
CHARGE_SETTLE_SECONDS = 2 * 60
DISCHARGE_SETTLE_SECONDS = 60
# a mouse's row in mice is made the first time it's seen, so a device that names a new unit id at each identify would add one a second without a limit of its own
NEW_MICE_BURST = 10
NEW_MICE_REFILL_SECONDS = 5 * 60
# a run that ends without its gap rows leaves at most this much of a mouse's old level standing, while an idle machine syncs the heartbeat to the disk only this often
HEARTBEAT_SECONDS = 5 * 60
# the wall clock moves against the boot clock only when it's set or slewed, so a move of more than this between two looks moves the rows this run saved along with it, while the slow drift of a normal slew stays under it
CLOCK_STEP_SECONDS = 2
# the probe saw a wake's battery event come with or after its wireless status event, but only on two wakes, so a reading this close before a wake counts as part of it
WAKE_SET_SECONDS = 2
MILLIVOLT_STEP = 20
REPORTS_ITSELF_AFTER_SECONDS = 60
CLEAR_POINTS = 2
CORRUPTION_CODES = (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
FAILED_OPEN = "open"
FAILED_SAVE = "save"
FAILED_READ = "read"
FAILED_BUDGET = "budget "
FAILED_NEW_MICE = "new mice"

ORIGIN_EVENT = 0
ORIGIN_READ = 1
ORIGIN_WAKE = 2
ORIGIN_ASLEEP = 3
ORIGIN_GAP = 4
ORIGIN_UPOWER = 5
READING_ORIGINS = (ORIGIN_EVENT, ORIGIN_READ, ORIGIN_UPOWER)
MARKER_ORIGINS = (ORIGIN_WAKE, ORIGIN_ASLEEP, ORIGIN_GAP)
SOURCE_HIDPP = "hidpp"
SOURCE_UPOWER = "upower"

DISCHARGE = 0
CHARGE = 1
CHARGING_CODES = (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW)
HISTORY_VERSION = 1
# a drop belongs to the bucket its start falls in, 80 to 71 down to 30 to 21, and has to end at 20 or above
DRAIN_BUCKETS = (80, 70, 60, 50, 40, 30)
# a rise belongs to the bucket its start falls in, 20 to 29 up to 70 to 79, and has to end at 80 or below
CHARGE_BUCKETS = (20, 30, 40, 50, 60, 70)
SESSION_STEPS = 3
BASELINE_SESSIONS = 20
WINDOW_SESSIONS = 20
WINDOWS = 3
LARGE_STEP = 5
LARGE_STEP_COUNT = 20
SESSION_DAYS = 90
SESSION_LIMIT = 500
SLOTS = 84
SLOT_SECONDS = 2 * 60 * 60
# each range's slot count and length, where None is a calendar month
LEVEL_RANGES = {"day": (48, 30 * 60), "week": (SLOTS, SLOT_SECONDS), "month": (60, 12 * 60 * 60), "halfYear": (26, 7 * 24 * 60 * 60), "year": (52, 7 * 24 * 60 * 60), "all": (120, None)}
# a long slot's level at its end says little, so these hold the lowest and highest level in each slot
SPAN_RANGES = ("month", "halfYear", "year", "all")
LEVELS_VERSION = 1
# a reading's height on the chart, which is its percent, or its rough level's height, or its millivolts for a mouse that reports only voltage
VALUE_SQL = "coalesce(percent, case level " + " ".join("when '" + name + "' then " + str(height) for name, height in hidpp.LEVEL_HEIGHTS.items()) + " end, millivolts)"
SESSION_COLUMNS = ("kind", "startAt", "endAt", "startPercent", "endPercent", "power", "readingCount", "slowCount", "errorCount", "inferred")
# a mouse's last reading from at to millivolts, its last power, what its open session's own row doesn't hold, and its newest row of any kind
TRACK_COLUMNS = ("session", "readingCount", "at", "origin", "percent", "level", "charging", "power", "millivolts", "lastPower", "lastPercent", "lowest", "powers", "newestAt", "newestOrigin")

SCHEMA = (
    "create table mice (id integer primary key, key text not null unique, name text, source text not null, firstSeen integer not null, reportsItselfSince integer, wakeSupport integer not null default 0)",
    "create table readings (mouseId integer not null, at integer not null, seq integer not null, percent integer, level text, charging integer, power integer, millivolts integer, origin integer not null, primary key (mouseId, at, seq)) without rowid",
    # session isn't in the spec's column list, and it's what lets a bucket take one figure per session without joining on time ranges
    "create table steps (mouseId integer not null, kind integer not null, at integer not null, seq integer not null, minutesPerPercent real not null, fromPercent integer not null, toPercent integer not null, power integer, unbroken integer not null, session integer not null, primary key (mouseId, kind, at, seq)) without rowid",
    "create table sessions (id integer primary key, mouseId integer not null, kind integer not null, startAt integer not null, endAt integer, startPercent integer, endPercent integer, power integer, readingCount integer not null, slowCount integer not null, errorCount integer not null, inferred integer not null)",
    "create index sessionsByMouse on sessions (mouseId, startAt)",
)


class CorruptHistory(sqlite3.DatabaseError):
    # quick_check reports corruption as rows instead of raising, so it's turned into this
    pass


def isCorruption(error):
    if (isinstance(error, CorruptHistory)):
        return True
    # Python's own sqlite3 errors, such as one on a closed connection, carry no code
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in CORRUPTION_CODES


def describe(error):
    return getattr(error, "sqlite_errorname", None) or str(error) or type(error).__name__


@dataclass(frozen=True)
class Row:
    at: int
    origin: int
    percent: int | None = None
    level: str | None = None
    charging: int | None = None
    power: int | None = None
    millivolts: int | None = None
    # the monotonic moment a row came in, which the wake set and the hold count from, so a wall clock that steps back can't hold them up
    arrived: float | None = field(default=None, compare=False)
    # the boot clock when the row came in, which counts suspend as the wall clock does but never jumps when the wall clock is set, so a length inside one run comes from it
    elapsed: float | None = field(default=None, compare=False)


def readingRow(reading, origin, at, arrived, elapsed):
    return Row(int(at), origin, reading.percent, reading.level, reading.charging, reading.power, reading.millivolts, arrived, elapsed)


def secondsBetween(startAt, startElapsed, endAt, endElapsed):
    # a row from an earlier run, or from a recorder without a boot clock, has none, so only the wall clock can measure from it
    if (startElapsed is None or endElapsed is None):
        return endAt - startAt
    return endElapsed - startElapsed


def bootClock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def savedTrack(db, mouseId):
    # an older release writes rows without updating the saved state, which the open session's id and reading count then no longer match
    found = db.execute("select " + ", ".join("tracks." + name for name in TRACK_COLUMNS) + " from tracks join sessions on sessions.id = tracks.session where tracks.mouseId = ? and sessions.endAt is null and sessions.readingCount = tracks.readingCount", (mouseId,)).fetchone()
    if (found is None):
        return None
    saved = dict(zip(TRACK_COLUMNS, found))
    try:
        saved["powers"] = Counter({power: count for power, count in json.loads(saved["powers"])})
    except (ValueError, TypeError):
        return None
    return saved


def newestRow(db, mouseId):
    saved = savedTrack(db, mouseId)
    if (saved is not None):
        return (saved["newestAt"], saved["newestOrigin"])
    return db.execute("select at, origin from readings where mouseId = ? order by at desc, seq desc limit 1", (mouseId,)).fetchone()


def differs(row, saved):
    if (saved is None):
        return True
    if ((row.percent, row.level, row.charging) != (saved.percent, saved.level, saved.charging)):
        return True
    # a power of None means the report didn't carry the byte, which says nothing about a change
    if (row.power is not None and saved.power is not None and row.power != saved.power):
        return True
    return row.millivolts is not None and saved.millivolts is not None and abs(row.millivolts - saved.millivolts) >= MILLIVOLT_STEP


def changesCodeOrPower(row, saved):
    if (saved is None):
        return True
    if (row.charging != saved.charging):
        return True
    return row.power is not None and saved.power is not None and row.power != saved.power


class Store:
    def __init__(self, folder, clock=time.time, monotonic=time.monotonic):
        self.folder = folder
        self.clock = clock
        self.monotonic = monotonic
        self.db = None
        self.tooNew = False
        # a history that kept turning corrupt serves none until the next start
        self.brokenThisRun = False
        self.corruptCount = 0
        # corruption found during a run runs the full check at the reopen, even when the flag file couldn't be written
        self.checkFully = False
        self.lastAttempt = None
        # the last run's gaps go in before anything this open saves, since a row saved first would sit where the gap belongs
        self.caughtUp = False
        self.catchUpAt = None
        # counts the opens, so a mouse loaded from an earlier one is loaded again
        self.opened = 0
        self.beatAt = None
        self.printed = set()
        # the lines each kind of failure still going on has printed, so its end gets a line and the next outage prints again
        self.failing = {}

    def path(self, suffix=""):
        return os.path.join(self.folder, FILE_NAME + suffix)

    def say(self, line, kind=None):
        if (kind is not None):
            self.failing.setdefault(kind, set()).add(line)
        if (line in self.printed):
            return
        self.printed.add(line)
        printLine(line)

    def works(self, kind, line):
        lines = self.failing.pop(kind, None)
        if (lines is None):
            return
        self.printed -= lines
        printLine(line)

    def isOpen(self):
        return self.db is not None

    def canSave(self):
        return self.db is not None and self.caughtUp

    def neverOpens(self):
        return self.folder is None or self.tooNew or self.brokenThisRun

    def retryAt(self):
        # the monotonic time the open or the catch up is tried again, or None when nothing waits on one
        if (self.neverOpens()):
            return None
        if (self.db is None):
            return None if self.lastAttempt is None else self.lastAttempt + OPEN_RETRY_SECONDS
        if (not self.caughtUp):
            return self.catchUpAt + OPEN_RETRY_SECONDS
        return None

    def ensureOpen(self):
        if (self.db is not None):
            return True
        if (self.neverOpens()):
            return False
        now = self.monotonic()
        if (self.lastAttempt is not None and now - self.lastAttempt < OPEN_RETRY_SECONDS):
            return False
        self.lastAttempt = now
        return self.open()

    def ensureReady(self):
        if (not self.ensureOpen()):
            return False
        if (self.caughtUp):
            return True
        if (self.monotonic() - self.catchUpAt < OPEN_RETRY_SECONDS):
            return False
        try:
            return self.catchUp()
        except sqlite3.Error as error:
            self.markCorrupt(error)
            return False

    def open(self):
        try:
            return self.openOnce()
        except sqlite3.Error as error:
            if (not isCorruption(error)):
                self.closeQuietly()
                self.say("Couldn't open the history: " + describe(error), FAILED_OPEN)
                return False
        # closing a connection deletes the write log beside the file, so the files move aside while it's still open
        renamed = self.renameBroken()
        self.closeQuietly()
        if (not renamed):
            return False
        try:
            return self.openOnce()
        except sqlite3.Error as error:
            self.closeQuietly()
            self.say("Couldn't start a new history: " + describe(error), FAILED_OPEN)
            return False

    def openOnce(self):
        flag = os.path.join(self.folder, INTEGRITY_FLAG)
        fullCheck = self.checkFully or os.path.exists(flag)
        # autocommit, so every transaction here is an explicit BEGIN, schema creation included
        self.db = sqlite3.connect(self.path(), timeout=1.0, isolation_level=None)
        rows = self.db.execute("pragma integrity_check" if fullCheck else "pragma quick_check").fetchall()
        if (rows != [("ok",)]):
            raise CorruptHistory("the check found " + str(rows[:3]))
        version = self.db.execute("pragma user_version").fetchone()[0]
        if (version > SCHEMA_VERSION):
            self.tooNew = True
            self.closeQuietly()
            self.say("The history was written by a newer version of Mouse Battery, so it's left as it is")
            return False
        self.db.execute("pragma journal_mode=wal")
        # a laptop can lose power with the history open, and FULL keeps every commit that returned, at the cost of a sync per commit, which the write budget keeps rare
        self.db.execute("pragma synchronous=full")
        if (version == 0):
            with self.transaction():
                for statement in SCHEMA:
                    self.db.execute(statement)
                self.db.execute("pragma user_version = " + str(SCHEMA_VERSION))
        self.opened += 1
        self.caughtUp = False
        self.catchUp()
        if (fullCheck):
            try:
                os.remove(flag)
            except OSError:
                pass
        self.checkFully = False
        self.lastAttempt = None
        self.works(FAILED_OPEN, "The history is open again")
        return True

    def catchUp(self):
        self.catchUpAt = self.monotonic()
        try:
            # one transaction, so a failure keeps the old heartbeat for the retry to find
            with self.transaction():
                # a build from earlier in 1.2.0's work saved clock offsets here and moved a boot's rows by them at its next start, which would move rows saved since then a second time
                self.db.execute("drop table if exists boots")
                # older releases ignore a table they don't know, so this one comes without a new version, which would lock them out of the file
                self.db.execute("create table if not exists tracks (mouseId integer primary key, session integer not null, readingCount integer not null, at integer not null, origin integer not null, percent integer, level text, charging integer, power integer, millivolts integer, lastPower integer, lastPercent integer, lowest integer, powers text not null, newestAt integer not null, newestOrigin integer not null)")
                self.recover(int(self.clock()))
        except sqlite3.Error as error:
            if (isCorruption(error)):
                raise
            # a disk that's full or read only, or another process holding the file, can still serve the history it has while the retry waits
            self.say("Couldn't save to the history: " + describe(error), FAILED_SAVE)
            return False
        self.caughtUp = True
        self.beatAt = self.monotonic()
        return True

    def recover(self, now):
        # older releases ignore a table they don't know, so this one comes without a new version, which would lock them out of the file
        self.db.execute("create table if not exists heartbeat (id integer primary key, at integer not null)")
        found = self.db.execute("select at from heartbeat where id = 0").fetchone()
        if (found is not None):
            self.addRecoveryGaps(found[0], now)
        self.db.execute("insert or replace into heartbeat (id, at) values (0, ?)", (now,))

    def addRecoveryGaps(self, savedAt, now):
        # a run that ended without its gap rows, on a power loss or a kill, would leave each mouse's last level standing over the time nothing watched, so a gap goes where that run was last seen
        for (mouseId,) in self.db.execute("select id from mice").fetchall():
            last = newestRow(self.db, mouseId)
            if (last is None or last[1] == ORIGIN_GAP):
                continue
            # a heartbeat saved while the wall clock was ahead would put the gap after the rows this run saves
            at = min(max(savedAt, last[0]), now)
            DbWriter(self.db, mouseId).insertReading(Row(at, ORIGIN_GAP))
            self.db.execute("update tracks set newestAt = ?, newestOrigin = ? where mouseId = ?", (at, ORIGIN_GAP, mouseId))

    def beat(self):
        # the attempt counts even when it fails, so a full disk is tried only as often as a working one
        self.beatAt = self.monotonic()
        try:
            with self.transaction():
                self.db.execute("insert or replace into heartbeat (id, at) values (0, ?)", (int(self.clock()),))
        except sqlite3.Error as error:
            if (isCorruption(error)):
                self.markCorrupt(error)
            else:
                self.say("Couldn't save to the history: " + describe(error), FAILED_SAVE)

    def renameBroken(self):
        suffix = ".broken-" + str(int(self.clock()))
        try:
            # the write log keeps the name SQLite looks for beside the renamed file, so the two stay a pair
            for extension in ("", "-wal", "-shm"):
                if (os.path.exists(self.path(extension))):
                    os.replace(self.path(extension), self.path(suffix + extension))
        except OSError as error:
            self.say("Couldn't move the broken history aside: " + str(error), FAILED_OPEN)
            return False
        self.say("The history was corrupt, so it was kept as " + FILE_NAME + suffix + " and a new one was started")
        return True

    def markCorrupt(self, error):
        self.closeQuietly()
        self.corruptCount += 1
        self.checkFully = True
        # the reopen waits like a failed open, and runs the same full check and move aside a start would
        self.lastAttempt = self.monotonic()
        if (self.corruptCount >= CORRUPT_REOPENS):
            self.brokenThisRun = True
            self.say("The history turned corrupt " + str(self.corruptCount) + " times in this run (" + describe(error) + "), so none is kept until Mouse Battery starts again")
        else:
            self.say("The history is corrupt (" + describe(error) + "), so it's closed and checked again in " + str(OPEN_RETRY_SECONDS // 60) + " minutes", FAILED_OPEN)
        try:
            with open(os.path.join(self.folder, INTEGRITY_FLAG), "w"):
                pass
        except OSError:
            pass

    def closeQuietly(self):
        if (self.db is None):
            return
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        self.db = None

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("begin immediate")
        try:
            yield self.db
            self.db.execute("commit")
        except BaseException:
            try:
                self.db.execute("rollback")
            except sqlite3.Error:
                pass
            raise
        self.works(FAILED_SAVE, "The history is saving again")


class DbWriter:
    def __init__(self, db, mouseId):
        self.db = db
        self.mouseId = mouseId

    def insertReading(self, row):
        # a clock that steps back can land on a second that already has rows, so the next free seq is looked up instead of counted
        self.db.execute("insert into readings (mouseId, at, seq, percent, level, charging, power, millivolts, origin) values (?, ?, (select coalesce(max(seq) + 1, 0) from readings where mouseId = ? and at = ?), ?, ?, ?, ?, ?, ?)", (self.mouseId, row.at, self.mouseId, row.at, row.percent, row.level, row.charging, row.power, row.millivolts, row.origin))

    def insertSession(self, session):
        cursor = self.db.execute("insert into sessions (mouseId, kind, startAt, endAt, startPercent, endPercent, power, readingCount, slowCount, errorCount, inferred) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (self.mouseId, session.kind, session.startAt, session.endAt, session.startPercent, session.endPercent, session.power(), session.readingCount, session.slowCount, session.errorCount, int(session.inferred)))
        return cursor.lastrowid

    def updateSession(self, session):
        self.db.execute("update sessions set startPercent = ?, endAt = ?, endPercent = ?, power = ?, readingCount = ?, slowCount = ?, errorCount = ? where id = ?", (session.startPercent, session.endAt, session.endPercent, session.power(), session.readingCount, session.slowCount, session.errorCount, session.id))

    def insertStep(self, kind, at, minutesPerPercent, fromPercent, toPercent, power, unbroken, sessionId):
        self.db.execute("insert into steps (mouseId, kind, at, seq, minutesPerPercent, fromPercent, toPercent, power, unbroken, session) values (?, ?, ?, (select coalesce(max(seq) + 1, 0) from steps where mouseId = ? and kind = ? and at = ?), ?, ?, ?, ?, ?, ?)", (self.mouseId, kind, at, self.mouseId, kind, at, minutesPerPercent, fromPercent, toPercent, power, int(unbroken), sessionId))

    def updateReportsItself(self, since):
        self.db.execute("update mice set reportsItselfSince = ? where id = ?", (since, self.mouseId))

    def saveTrack(self, track, newest):
        # a reboot can set the clock back behind rows already saved, so loading takes the last reading and the open session from here instead of from the newest dates
        session = track.session
        last = track.last
        if (session is None or last is None):
            return
        self.db.execute("insert or replace into tracks (mouseId, " + ", ".join(TRACK_COLUMNS) + ") values (?" + ", ?" * len(TRACK_COLUMNS) + ")", (self.mouseId, session.id, session.readingCount, last.at, last.origin, last.percent, last.level, last.charging, last.power, last.millivolts, track.lastPower, session.lastPercent, session.lowest, json.dumps(list(session.powers.items())), newest.at, newest.origin))

    def shift(self, since, moved):
        # each row goes to the next free seq of its new second in the order it was saved, so rows that land on one second keep their order
        for at, seq in self.db.execute("select at, seq from readings where mouseId = ? and at >= ? order by at, seq", (self.mouseId, since)).fetchall():
            self.db.execute("update readings set at = ?, seq = (select coalesce(max(seq) + 1, 0) from readings where mouseId = ? and at = ?) where mouseId = ? and at = ? and seq = ?", (at + moved, self.mouseId, at + moved, self.mouseId, at, seq))
        for kind in (DISCHARGE, CHARGE):
            for at, seq in self.db.execute("select at, seq from steps where mouseId = ? and kind = ? and at >= ? order by at, seq", (self.mouseId, kind, since)).fetchall():
                self.db.execute("update steps set at = ?, seq = (select coalesce(max(seq) + 1, 0) from steps where mouseId = ? and kind = ? and at = ?) where mouseId = ? and kind = ? and at = ? and seq = ?", (at + moved, self.mouseId, kind, at + moved, self.mouseId, kind, at, seq))
        self.db.execute("update sessions set startAt = startAt + ? where mouseId = ? and startAt >= ?", (moved, self.mouseId, since))
        self.db.execute("update sessions set endAt = endAt + ? where mouseId = ? and endAt >= ?", (moved, self.mouseId, since))
        self.db.execute("update mice set firstSeen = firstSeen + ? where id = ? and firstSeen >= ?", (moved, self.mouseId, since))
        self.db.execute("update mice set reportsItselfSince = reportsItselfSince + ? where id = ? and reportsItselfSince >= ?", (moved, self.mouseId, since))
        self.db.execute("update tracks set at = at + ? where mouseId = ? and at >= ?", (moved, self.mouseId, since))
        self.db.execute("update tracks set newestAt = newestAt + ? where mouseId = ? and newestAt >= ?", (moved, self.mouseId, since))


class NullWriter:
    # the in memory rules still run for a mouse with no history, so its reports on its own flag stays right
    def __init__(self):
        self.nextId = -1

    def insertReading(self, row):
        pass

    def insertSession(self, session):
        self.nextId -= 1
        return self.nextId

    def updateSession(self, session):
        pass

    def insertStep(self, *args):
        pass

    def updateReportsItself(self, since):
        pass


class Session:
    def __init__(self, kind, row, fromCode=False, inferred=False):
        self.id = None
        self.kind = kind
        self.startAt = row.at
        self.startElapsed = row.elapsed
        self.startPercent = row.percent
        self.endAt = None
        self.endPercent = None
        self.lastAt = row.at
        self.lastPercent = row.percent
        self.powers = Counter()
        self.readingCount = 0
        self.slowCount = 0
        self.errorCount = 0
        self.fromCode = fromCode
        self.inferred = inferred
        self.lowest = None

    def power(self):
        if (not self.powers):
            return None
        return self.powers.most_common(1)[0][0]

    def count(self, row):
        self.readingCount += 1
        if (row.charging == hidpp.CHARGING_SLOW):
            self.slowCount += 1
        if (row.charging == hidpp.CHARGING_ERROR):
            self.errorCount += 1
        if (row.power is not None):
            self.powers[row.power] += 1
        self.lastAt = row.at
        if (row.percent is not None):
            self.lastPercent = row.percent
            self.lowest = row.percent if self.lowest is None else min(self.lowest, row.percent)


class Step:
    def __init__(self, row, live, lowestBefore):
        self.startPercent = row.percent
        self.startAt = row.at
        self.startElapsed = row.elapsed
        # the start row came in at the moment its percent was reached
        self.live = live
        # a wake or a sleep fell inside the step
        self.broken = False
        self.lowestBefore = lowestBefore
        self.power = row.power

    def seePower(self, power):
        if (power is None):
            return
        if (self.power is None or power != 0):
            self.power = power


class Track:
    def __init__(self, wakeSupport=False, reportsItselfSince=None):
        self.wakeSupport = wakeSupport
        self.reportsItselfSince = reportsItselfSince
        # the receiver doesn't know the wireless status index yet, so it can't see a wake, whatever the saved flag says
        self.wakesUnseen = False
        # the last saved row that holds a reading, which every rule compares against
        self.last = None
        # a wake, asleep, or gap row came after it
        self.boundary = True
        self.asleep = False
        self.lastWakeOrGap = None
        self.lastWakeOrGapElapsed = None
        self.session = None
        self.step = None
        self.rises = 0
        self.lastPower = None

    def copy(self):
        twin = copy.copy(self)
        if (self.session is not None):
            twin.session = copy.copy(self.session)
            twin.session.powers = Counter(self.session.powers)
        if (self.step is not None):
            twin.step = copy.copy(self.step)
        return twin

    def shift(self, since, moved):
        # only the dates this run saved moved in the database, so only those move here
        def follow(at):
            return at + moved if at is not None and at >= since else at

        if (self.last is not None):
            self.last = replace(self.last, at=follow(self.last.at))
        self.lastWakeOrGap = follow(self.lastWakeOrGap)
        self.reportsItselfSince = follow(self.reportsItselfSince)
        if (self.session is not None):
            self.session.startAt = follow(self.session.startAt)
            self.session.lastAt = follow(self.session.lastAt)
            self.session.endAt = follow(self.session.endAt)
        if (self.step is not None):
            self.step.startAt = follow(self.step.startAt)

    def applyMarker(self, row, writer):
        writer.insertReading(row)
        self.boundary = True
        # a gap ends the step in progress without saving it, while a wake or a sleep leaves it to be saved as not whole
        if (row.origin == ORIGIN_GAP):
            self.step = None
        elif (self.step is not None):
            self.step.broken = True
        self.rises = 0
        if (row.origin == ORIGIN_WAKE):
            self.asleep = False
            self.lastWakeOrGap = row.at
            self.lastWakeOrGapElapsed = row.elapsed
        elif (row.origin == ORIGIN_ASLEEP):
            self.asleep = True
        else:
            self.lastWakeOrGap = row.at
            self.lastWakeOrGapElapsed = row.elapsed

    def applyReading(self, row, writer):
        writer.insertReading(row)
        previous = self.last
        boundary = self.boundary
        self.updateReportsItself(row, previous, writer)
        session = self.session
        if (session is None):
            self.startSession(row, previous, boundary, writer, CHARGE if row.charging in CHARGING_CODES else DISCHARGE, fromCode=row.charging in CHARGING_CODES)
        elif (session.kind == DISCHARGE):
            rise = None if (previous is None or previous.percent is None or row.percent is None) else row.percent - previous.percent
            if (row.charging in CHARGING_CODES):
                # a charging code wins over the rise rules, so no inferred charge is saved and this one starts normally
                self.endSession(writer)
                self.startSession(row, previous, boundary, writer, CHARGE, fromCode=True)
            elif (boundary and rise is not None and rise >= 5):
                # a charge done while the mouse slept or the service was down shows only its two ends
                self.endSession(writer)
                self.saveChargeAcross(previous, row, writer)
                self.startSession(row, previous, boundary, writer, DISCHARGE)
            elif (not boundary and self.risesToCharge(rise)):
                self.endSession(writer)
                self.startSession(row, previous, boundary, writer, CHARGE, inferred=True)
            else:
                self.addToSession(row, previous, boundary, writer)
        elif (self.chargeEnds(previous, row)):
            self.endSession(writer)
            self.startSession(row, previous, boundary, writer, DISCHARGE)
        else:
            self.addToSession(row, previous, boundary, writer)
        if (row.power is not None):
            self.lastPower = row.power
        self.last = row
        self.boundary = False

    def seesWakes(self):
        return self.wakeSupport and not self.wakesUnseen

    def risesToCharge(self, rise):
        if (rise is None):
            self.rises = 0
            return False
        # without wake rows a rebound after a rest can't be told from a charge, so those mice need more
        needed = 2 if self.seesWakes() else 5
        inARow = 2 if self.seesWakes() else 3
        if (rise >= needed):
            self.rises = 0
            return True
        if (rise == 1):
            self.rises += 1
            if (self.rises >= inARow):
                self.rises = 0
                return True
            return False
        if (rise != 0):
            self.rises = 0
        return False

    def chargeEnds(self, previous, row):
        session = self.session
        if (row.charging == hidpp.CHARGING_FULL):
            return True
        if (row.charging == hidpp.CHARGING_DISCHARGING and session.fromCode):
            return True
        # a power of None never counts as a change, so only a known nonzero going to 0 does
        if (row.power == 0 and self.lastPower not in (None, 0)):
            return True
        if (session.inferred and previous is not None and previous.percent is not None and row.percent is not None and row.percent < previous.percent):
            return True
        return False

    def startSession(self, row, previous, boundary, writer, kind, fromCode=False, inferred=False):
        session = Session(kind, row, fromCode, inferred)
        session.count(row)
        session.id = writer.insertSession(session)
        self.session = session
        self.rises = 0
        self.step = self.newStep(row, previous, boundary, None)

    def saveChargeAcross(self, previous, row, writer):
        session = Session(CHARGE, previous, inferred=True)
        session.endAt = row.at
        session.endPercent = row.percent
        writer.insertSession(session)

    def endSession(self, writer):
        session = self.session
        # a session ends at its own last saved reading, and the reading that ended it starts the next one
        session.endAt = session.lastAt
        session.endPercent = session.lastPercent
        writer.updateSession(session)
        self.session = None
        self.step = None

    def newStep(self, row, previous, boundary, lowestBefore):
        if (row.percent is None):
            return None
        live = previous is not None and previous.percent is not None and previous.percent != row.percent and not boundary
        return Step(row, live, lowestBefore)

    def addToSession(self, row, previous, boundary, writer):
        session = self.session
        lowestBefore = session.lowest
        session.count(row)
        # a reading that fell in a discharge's first minute shows its start was the level settling, such as a full reading counting down to the real one, so the start follows it
        if (session.kind == DISCHARGE and row.percent is not None and session.startPercent is not None and row.percent < session.startPercent and 0 <= secondsBetween(session.startAt, session.startElapsed, row.at, row.elapsed) <= DISCHARGE_SETTLE_SECONDS):
            session.startPercent = row.percent
        writer.updateSession(session)
        if (row.percent is None):
            return
        step = self.step
        if (step is None):
            self.step = self.newStep(row, previous, boundary, lowestBefore)
            return
        if (row.percent == step.startPercent):
            # a change of code or power at the same percent stays inside the step
            step.seePower(row.power)
            return
        self.saveStep(step, row, writer)
        self.step = self.newStep(row, previous, boundary, lowestBefore)

    def saveStep(self, step, row, writer):
        session = self.session
        moved = row.percent - step.startPercent
        if ((moved < 0 and session.kind != DISCHARGE) or (moved > 0 and session.kind != CHARGE)):
            return
        minutes = secondsBetween(step.startAt, step.startElapsed, row.at, row.elapsed) / 60
        # a clock that stepped back or stood still gives no length to measure
        if (minutes <= 0):
            return
        whole = step.live and not step.broken
        sinceStart = secondsBetween(session.startAt, session.startElapsed, step.startAt, step.startElapsed)
        if (moved < 0):
            newLow = step.lowestBefore is None or step.startPercent < step.lowestBefore
            # a discharge's first minute can count down from a full reading to the real level, so those steps aren't measured
            unbroken = whole and self.seesWakes() and newLow and not step.power and sinceStart >= DISCHARGE_SETTLE_SECONDS
            kind = DISCHARGE
        else:
            # a charge's first minutes can rise faster than the battery fills while the level catches up, so those steps aren't measured
            unbroken = whole and sinceStart >= CHARGE_SETTLE_SECONDS
            kind = CHARGE
        writer.insertStep(kind, row.at, minutes / abs(moved), step.startPercent, row.percent, step.power, unbroken, session.id)

    def updateReportsItself(self, row, previous, writer):
        if (self.reportsItselfSince is not None or row.origin != ORIGIN_EVENT or not self.seesWakes()):
            return
        if (self.session is None or self.session.kind != DISCHARGE):
            return
        if (previous is None or previous.percent is None or row.percent is None or row.percent >= previous.percent):
            return
        if (self.lastWakeOrGap is not None and secondsBetween(self.lastWakeOrGap, self.lastWakeOrGapElapsed, row.at, row.elapsed) < REPORTS_ITSELF_AFTER_SECONDS):
            return
        self.reportsItselfSince = row.at
        writer.updateReportsItself(row.at)


class MouseEntry:
    def __init__(self, key, persist):
        self.key = key
        self.persist = persist
        self.name = None
        self.source = None
        self.mouseId = None
        self.loaded = False
        self.track = Track()
        self.pending = []
        self.held = None
        self.lastWriteAt = None
        self.revision = 0
        self.needsGap = False
        self.budget = WRITE_BURST
        self.budgetAt = None
        # the name often comes after the mouse's row was made, so it's saved with the next write
        self.nameChanged = False
        # the newest reading seen, saved or not, and whether a marker came after it, for the rule that clears reports on its own
        self.latest = None
        self.latestBroken = True
        # flag changes memory already has but the database doesn't, which the next transaction that succeeds saves, load's included
        self.pendingFlags = {}
        # the first date saved this run and the newest one saved before it, so a step of the wall clock moves this run's rows and nothing older, which only works while the two don't overlap
        self.runTracked = False
        self.runSince = None
        self.runAfter = None
        # the newest row memory took that the database doesn't have, which the retry saves, since an unchanged reading never reaches a write
        self.unsaved = None
        self.retryAt = None


class Recorder(QObject):
    changed = pyqtSignal(str)

    def __init__(self, folder=None, clock=time.time, timers=True, monotonic=time.monotonic, boottime=None):
        super().__init__()
        # the wall clock dates the rows, while every wait runs on the monotonic one
        self.clock = clock
        self.monotonic = monotonic
        # the real boot clock only measures the same lengths as the real wall clock, so a wall clock passed in has to bring its own
        if (boottime is None and clock is time.time and hasattr(time, "CLOCK_BOOTTIME")):
            boottime = bootClock
        self.boottime = boottime
        # the wall clock minus the boot clock at the last look, which moves only when the wall clock is set
        self.offset = None
        self.store = Store(folder, clock, monotonic) if folder is not None else None
        # the store's open this recorder's loaded mice came from
        self.opened = 0
        self.entries = {}
        # one count for the whole recorder, so a mouse forgotten and seen again never gets a revision an old reply already has
        self.revisions = 0
        self.newMice = NEW_MICE_BURST
        self.newMiceAt = None
        self.timer = None
        if (timers):
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.tick)

    def available(self):
        if (self.store is None):
            return False
        if (not self.store.ensureOpen()):
            # the retry runs on the timer too, so the last run's gaps go in once the disk frees up even with no mouse around
            self.schedule()
            return False
        self.followOpens()
        return True

    def ready(self):
        if (not self.available()):
            return False
        if (not self.store.ensureReady()):
            self.schedule()
            return False
        return True

    def markCorrupt(self, error):
        self.store.markCorrupt(error)
        self.schedule()

    def followOpens(self):
        if (self.opened == self.store.opened):
            return
        self.opened = self.store.opened
        # a reopen can bring another file, or the same one with a gap where it was closed, so each mouse loads again and saves the level memory holds
        for entry in self.entries.values():
            if (entry.loaded):
                self.unload(entry)
        self.schedule()

    def unload(self, entry):
        entry.loaded = False
        entry.needsGap = True
        if (entry.unsaved is None and not entry.track.boundary and entry.track.last is not None):
            entry.unsaved = entry.track.last

    def entryFor(self, key, pathKey=False):
        entry = self.entries.get(key)
        if (entry is None):
            # a key built from a path can pass to a different mouse later, so it gets no history
            entry = MouseEntry(key, not pathKey)
            self.entries[key] = entry
        return entry

    def forget(self, key, source=SOURCE_HIDPP):
        # a mouse that left is dropped after its rows go out, so identities that come and go don't pile up, and one that comes back loads its saved flags again
        self.followClock()
        entry = self.entries.get(key)
        if (entry is None or (entry.source is not None and entry.source != source)):
            return
        pending = entry.pending
        entry.pending = []
        for row in pending:
            self.process(entry, row)
        self.flushHeld(entry)
        # a flag change or a row the database hasn't taken yet would be lost, so that entry stays for the retry, unless no save ever will or the mouse has no row to save it to
        if ((entry.pendingFlags or entry.unsaved is not None) and entry.persist and entry.mouseId is not None and self.store is not None and not self.store.neverOpens()):
            self.schedule()
            return
        del self.entries[key]
        self.schedule()

    def reportsItself(self, key):
        entry = self.entries.get(key)
        return entry is not None and entry.track.reportsItselfSince is not None

    def revision(self, key):
        entry = self.entries.get(key)
        return 0 if entry is None else entry.revision

    def summary(self, key):
        self.followClock()
        if (not self.available()):
            raise HistoryUnavailable()
        try:
            summary = readSummary(self.store.db, self.entries.get(key), key, self.clock())
        except sqlite3.Error as error:
            if (isCorruption(error)):
                self.markCorrupt(error)
            else:
                self.store.say("Couldn't read the history: " + describe(error), FAILED_READ)
            raise HistoryUnavailable() from error
        self.store.works(FAILED_READ, "The history can be read again")
        return summary

    def levels(self, key, rangeName):
        if (rangeName not in LEVEL_RANGES):
            raise ValueError("unknown range " + repr(rangeName))
        self.followClock()
        if (not self.available()):
            raise HistoryUnavailable()
        try:
            levels = readLevels(self.store.db, key, rangeName, self.clock())
        except sqlite3.Error as error:
            if (isCorruption(error)):
                self.markCorrupt(error)
            else:
                self.store.say("Couldn't read the history: " + describe(error), FAILED_READ)
            raise HistoryUnavailable() from error
        self.store.works(FAILED_READ, "The history can be read again")
        return levels

    def claim(self, entry, source):
        # one mouse never gets two histories, so UPower's rows for a mouse read over HID++ are left out
        if (source == SOURCE_HIDPP):
            if (entry.source == SOURCE_UPOWER):
                self.takeFromUPower(entry)
            entry.source = SOURCE_HIDPP
            return True
        if (entry.source == SOURCE_HIDPP):
            return False
        if (entry.source is None):
            hidppKey = self.isHidppKey(entry)
            # a lookup that failed, or a history that isn't open yet, drops this reading and leaves the next one to ask again
            if (hidppKey is None):
                return False
            if (hidppKey):
                entry.source = SOURCE_HIDPP
                return False
        entry.source = SOURCE_UPOWER
        return True

    def takeFromUPower(self, entry):
        # UPower can name a mouse before the HID++ search does, so its rows still waiting are dropped and the saved source moves over
        entry.pending = []
        entry.held = None
        entry.latest = None
        entry.latestBroken = True
        entry.unsaved = None
        if (not entry.persist or not entry.loaded):
            return
        # a reopen in between unloads the mouse, and loading it again saves the source
        if (not self.ready() or not entry.loaded):
            return
        try:
            with self.store.transaction() as db:
                db.execute("update mice set source = ? where id = ?", (SOURCE_HIDPP, entry.mouseId))
        except sqlite3.Error as error:
            self.writeFailed(entry, error)
            # loading again saves the source once the database answers
            self.unload(entry)

    def setWakeSupport(self, key, supported, pathKey=False):
        # None is a lookup that didn't answer, which leaves the saved flag as it is and counts wakes as unseen until one answers
        self.followClock()
        entry = self.entryFor(key, pathKey)
        self.claim(entry, SOURCE_HIDPP)
        if (supported is None):
            entry.track.wakesUnseen = True
            self.ensureLoaded(entry)
            return
        if (entry.track.wakesUnseen and entry.track.step is not None):
            # the step in progress began while a sleep could pass unseen
            entry.track.step.broken = True
        entry.track.wakesUnseen = False
        entry.track.wakeSupport = supported
        entry.pendingFlags["wakeSupport"] = supported
        self.saveFlags(entry)

    def saveFlags(self, entry):
        if (not self.ensureLoaded(entry) or not entry.pendingFlags):
            return
        try:
            with self.store.transaction() as db:
                self.writeFlags(db, entry)
        except sqlite3.Error as error:
            self.writeFailed(entry, error)
            return
        entry.pendingFlags = {}

    def writeFlags(self, db, entry):
        if ("wakeSupport" in entry.pendingFlags):
            db.execute("update mice set wakeSupport = ? where id = ?", (int(entry.pendingFlags["wakeSupport"]), entry.mouseId))
        if ("reportsItselfSince" in entry.pendingFlags):
            DbWriter(db, entry.mouseId).updateReportsItself(entry.pendingFlags["reportsItselfSince"])

    def addReading(self, key, reading, origin, name=None, source=SOURCE_HIDPP, pathKey=False, at=None, arrived=None):
        self.followClock()
        at = self.clock() if at is None else at
        # a report carries the moment the worker read it, which can be well before the Qt thread gets to it
        arrived = self.monotonic() if arrived is None else arrived
        elapsed = self.bootNow()
        entry = self.entryFor(key, pathKey)
        if (not self.claim(entry, source)):
            return
        if (name and name != entry.name):
            entry.name = name
            entry.nameChanged = True
        row = readingRow(reading, origin, at, arrived, elapsed)
        if (origin == ORIGIN_READ):
            self.checkMissedStep(entry, row)
        entry.latest = row
        entry.latestBroken = False
        entry.pending.append(row)
        self.schedule()

    def bootNow(self):
        return None if self.boottime is None else self.boottime()

    def followClock(self):
        if (self.boottime is None):
            return
        offset = self.clock() - self.boottime()
        previous = self.offset
        # comparing each look with the one before keeps the slow slewing of the wall clock from adding up to a step
        self.offset = offset
        if (previous is None or abs(offset - previous) <= CLOCK_STEP_SECONDS):
            return
        moved = round(offset - previous)
        for entry in list(self.entries.values()):
            self.shiftEntry(entry, moved)

    def shiftEntry(self, entry, moved):
        # the rows waiting were dated on the wall clock before the step too
        entry.pending = [replace(row, at=row.at + moved) for row in entry.pending]
        if (entry.held is not None):
            entry.held = replace(entry.held, at=entry.held.at + moved)
        if (entry.unsaved is not None):
            entry.unsaved = replace(entry.unsaved, at=entry.unsaved.at + moved)
        if (not entry.loaded or not entry.runTracked or entry.runSince is None):
            return
        since = entry.runSince
        tracked = entry.runAfter is None or since + moved > entry.runAfter
        if (not self.store.canSave() or self.opened != self.store.opened):
            # the rows this run saved keep their dates, as rows do when the clock is set while nothing runs, and loading again tracks the run from its next row
            entry.track.shift(since, moved)
            self.unload(entry)
            return
        try:
            with self.store.transaction() as db:
                DbWriter(db, entry.mouseId).shift(since, moved)
        except sqlite3.Error as error:
            entry.track.shift(since, moved)
            self.writeFailed(entry, error)
            # the rows kept their dates, so the track is loaded from them again
            self.unload(entry)
            return
        entry.track.shift(since, moved)
        if (entry.pendingFlags.get("reportsItselfSince") is not None and entry.pendingFlags["reportsItselfSince"] >= since):
            entry.pendingFlags["reportsItselfSince"] += moved
        entry.runSince = since + moved
        entry.runTracked = tracked
        # a reply cached before the move holds the old dates, so the move counts as a saved change
        self.revisions += 1
        entry.revision = self.revisions
        self.changed.emit(entry.key)

    def isHidppKey(self, entry):
        # a key only UPower has named this run is still a HID++ mouse when its saved history says so
        if (not entry.persist or self.store is None):
            return False
        if (not self.available()):
            # a history that may still open could say the key is HID++, so the reading is dropped and the next one asks again
            return False if self.store.neverOpens() else None
        try:
            found = self.store.db.execute("select source from mice where key = ?", (entry.key,)).fetchone()
        except sqlite3.Error as error:
            if (isCorruption(error)):
                self.markCorrupt(error)
            else:
                self.store.say("Couldn't read the history: " + describe(error), FAILED_READ)
            return None
        self.store.works(FAILED_READ, "The history can be read again")
        return found is not None and found[0] == SOURCE_HIDPP

    def checkMissedStep(self, entry, row):
        base = entry.latest
        # a read after a marker isn't compared, since the mouse may have charged elsewhere or while the PC was off
        if (entry.track.reportsItselfSince is None or base is None or entry.latestBroken):
            return
        if (base.percent is None or row.percent is None or abs(row.percent - base.percent) < CLEAR_POINTS):
            return
        # the mouse moved two points without a battery event, so it doesn't report every step after all
        entry.track.reportsItselfSince = None
        entry.pendingFlags["reportsItselfSince"] = None
        self.saveFlags(entry)

    def addMarker(self, key, origin, source=SOURCE_HIDPP, pathKey=False, at=None, arrived=None):
        self.followClock()
        at = int(self.clock() if at is None else at)
        arrived = self.monotonic() if arrived is None else arrived
        entry = self.entryFor(key, pathKey)
        if (not self.claim(entry, source)):
            return
        marker = Row(at, origin, arrived=arrived, elapsed=self.bootNow())
        if (origin == ORIGIN_WAKE):
            early = [row for row in entry.pending if row.arrived < arrived - WAKE_SET_SECONDS]
            late = [row for row in entry.pending if row.arrived >= arrived - WAKE_SET_SECONDS]
        else:
            early = list(entry.pending)
            late = []
        entry.pending = []
        for row in early:
            self.process(entry, row)
        self.flushHeld(entry)
        self.write(entry, marker)
        # a reading this close before the wake belongs to the wake set, so it goes in after the wake row and at its time
        for row in late:
            self.process(entry, Row(at, row.origin, row.percent, row.level, row.charging, row.power, row.millivolts, row.arrived, marker.elapsed))
        entry.latestBroken = True
        self.schedule()

    def tick(self):
        self.followClock()
        now = self.monotonic()
        for entry in list(self.entries.values()):
            while (entry.pending and entry.pending[0].arrived <= now - WAKE_SET_SECONDS):
                self.process(entry, entry.pending.pop(0))
            if (entry.held is not None and entry.lastWriteAt is not None and now >= entry.lastWriteAt + HOLD_SECONDS):
                self.flushHeld(entry)
        if (self.store is not None):
            retry = self.store.retryAt()
            if (retry is not None and now >= retry):
                self.ready()
            for entry in list(self.entries.values()):
                if (self.waitsToSave(entry) and self.store.canSave() and (entry.retryAt is None or now >= entry.retryAt)):
                    self.retrySave(entry, now)
        if (self.watching() and now >= self.store.beatAt + HEARTBEAT_SECONDS):
            self.store.beat()
        self.schedule()

    def waitsToSave(self, entry):
        # a mouse the new mice budget left out saves its flags with its next reading, so only a mouse with a row is retried for flags alone
        return entry.persist and (entry.unsaved is not None or (bool(entry.pendingFlags) and entry.mouseId is not None))

    def retrySave(self, entry, now):
        # a mouse whose readings don't change never reaches a write, so its row is saved here once the history takes saves again
        entry.retryAt = now + OPEN_RETRY_SECONDS
        row = entry.unsaved
        if (not self.ensureLoaded(entry)):
            return
        if (row is None):
            self.saveFlags(entry)
            return
        try:
            newest = newestRow(self.store.db, entry.mouseId)
        except sqlite3.Error as error:
            self.writeFailed(entry, error)
            return
        if (newest is not None):
            # the reopen's gap can go in after the row, and the level memory still holds goes after it, though never after now, since a row saved while the clock was ahead can be newer
            at = min(newest[0], int(self.clock()))
            if (at > row.at):
                row = replace(row, at=at)
                if (newest == (at, ORIGIN_GAP)):
                    entry.needsGap = False
        entry.unsaved = None
        self.write(entry, row)

    def watching(self):
        # a machine with no mouse in the history has nothing to keep a heartbeat for
        return self.store is not None and self.store.canSave() and any(entry.loaded for entry in self.entries.values())

    def schedule(self):
        if (self.timer is None):
            return
        due = []
        for entry in self.entries.values():
            if (entry.pending):
                due.append(entry.pending[0].arrived + WAKE_SET_SECONDS)
            if (entry.held is not None and entry.lastWriteAt is not None):
                due.append(entry.lastWriteAt + HOLD_SECONDS)
        if (self.watching()):
            due.append(self.store.beatAt + HEARTBEAT_SECONDS)
        if (self.store is not None):
            retry = self.store.retryAt()
            if (retry is not None):
                due.append(retry)
            if (self.store.canSave()):
                due += [self.monotonic() if entry.retryAt is None else entry.retryAt for entry in self.entries.values() if self.waitsToSave(entry)]
        if (not due):
            self.timer.stop()
            return
        wait = min(due) - self.monotonic()
        self.timer.start(max(1, int(wait * 1000) + 1))

    def stop(self):
        self.followClock()
        for entry in self.entries.values():
            pending = entry.pending
            entry.pending = []
            for row in pending:
                self.process(entry, row)
            self.flushHeld(entry)
        # a clean stop saves when it stopped, so a mouse whose source writes no gap row on the way out still gets one at the next start
        if (self.watching()):
            self.store.beat()
        if (self.timer is not None):
            self.timer.stop()
        if (self.store is not None):
            self.store.closeQuietly()

    def process(self, entry, row):
        saved = entry.track.last
        if (entry.track.boundary or saved is None):
            # the first reading after a marker is always written, so the level the mouse woke at is known
            self.write(entry, row)
            return
        if (entry.held is not None):
            if (not differs(row, saved)):
                # a reading that flickered away and came back leaves nothing behind
                entry.held = None
                return
            if (changesCodeOrPower(row, saved)):
                self.flushHeld(entry)
                self.write(entry, row)
                return
            entry.held = row
            return
        if (not differs(row, saved)):
            return
        if (changesCodeOrPower(row, saved) or entry.lastWriteAt is None or row.arrived - entry.lastWriteAt >= HOLD_SECONDS):
            self.write(entry, row)
            return
        entry.held = row

    def flushHeld(self, entry):
        if (entry.held is None):
            return
        row = entry.held
        entry.held = None
        self.write(entry, row)

    def ensureLoaded(self, entry):
        if (not entry.persist or not self.ready()):
            return False
        if (entry.loaded):
            return True
        try:
            self.load(entry)
        except sqlite3.Error as error:
            self.writeFailed(entry, error)
        return entry.loaded

    def load(self, entry):
        db = self.store.db
        found = db.execute("select id, reportsItselfSince, wakeSupport, source from mice where key = ?", (entry.key,)).fetchone()
        if (found is None and not self.takeNewMouse()):
            # the mouse waits in memory like one whose history is closed, and its first saved row follows a gap
            entry.needsGap = True
            self.store.say("More new mice showed up than the history adds at once, so some aren't saved yet", FAILED_NEW_MICE)
            return
        if (found is None):
            firstSeen = int(self.clock())
            with self.store.transaction():
                cursor = db.execute("insert into mice (key, name, source, firstSeen, reportsItselfSince, wakeSupport) values (?, ?, ?, ?, ?, ?)", (entry.key, entry.name, entry.source, firstSeen, entry.track.reportsItselfSince, int(entry.track.wakeSupport)))
            entry.mouseId = cursor.lastrowid
            entry.runSince = firstSeen
            entry.runAfter = None
            # a row made after some were left out means they're being saved again
            self.store.works(FAILED_NEW_MICE, "New mice are saved to the history again")
            track = Track(entry.track.wakeSupport, entry.track.reportsItselfSince)
        else:
            entry.mouseId = found[0]
            if (found[3] == SOURCE_HIDPP):
                entry.source = SOURCE_HIDPP
            track = Track(bool(found[2]), found[1])
            # a flag change made while the history wasn't open is newer than the saved one
            track.wakeSupport = entry.pendingFlags.get("wakeSupport", track.wakeSupport)
            track.reportsItselfSince = entry.pendingFlags.get("reportsItselfSince", track.reportsItselfSince)
            with self.store.transaction():
                db.execute("update mice set name = coalesce(?, name), source = ? where id = ?", (entry.name, entry.source, entry.mouseId))
                self.writeFlags(db, entry)
            saved = savedTrack(db, entry.mouseId)
            if (saved is not None):
                track.last = Row(*(saved[name] for name in ("at", "origin", "percent", "level", "charging", "power", "millivolts")))
                track.lastPower = saved["lastPower"]
            else:
                # a history an older release wrote last has no saved state to trust, so the newest dates stand in for the order the rows were saved in
                last = db.execute("select at, origin, percent, level, charging, power, millivolts from readings where mouseId = ? and origin in (0, 1, 5) order by at desc, seq desc limit 1", (entry.mouseId,)).fetchone()
                if (last is not None):
                    track.last = Row(*last)
                power = db.execute("select power from readings where mouseId = ? and power is not null order by at desc, seq desc limit 1", (entry.mouseId,)).fetchone()
                track.lastPower = None if power is None else power[0]
            track.session = self.loadOpenSession(db, entry.mouseId, saved, int(self.clock()), self.bootNow())
            # a step during this run moves only rows it saves, so nothing older than the newest date saved before it
            entry.runSince = None
            entry.runAfter = db.execute("select max(at) from readings where mouseId = ?", (entry.mouseId,)).fetchone()[0]
            # what happened while nothing was watching is unknown, so the first row after loading follows a gap row
            entry.needsGap = True
        entry.runTracked = True
        track.boundary = True
        track.wakesUnseen = entry.track.wakesUnseen
        entry.track = track
        entry.loaded = True
        entry.pendingFlags = {}

    def loadOpenSession(self, db, mouseId, saved, now, elapsed):
        found = db.execute("select id, kind, startAt, startPercent, readingCount, slowCount, errorCount, inferred from sessions where mouseId = ? and endAt is null order by id desc limit 1", (mouseId,)).fetchone()
        if (found is None):
            return None
        sessionId, kind, startAt, startPercent, readingCount, slowCount, errorCount, inferred = found
        session = Session(kind, Row(startAt, ORIGIN_READ, startPercent), inferred=bool(inferred))
        session.id = sessionId
        session.readingCount = readingCount
        session.slowCount = slowCount
        session.errorCount = errorCount
        # a charge a code started is the only kind a code 0 ends, and a saved charge that isn't inferred was started by one
        session.fromCode = (kind == CHARGE and not inferred)
        if (saved is not None and saved["session"] == sessionId):
            # every reading goes into the session open when it comes, so the session's last reading is the mouse's
            session.powers = saved["powers"]
            session.lowest = saved["lowest"]
            session.lastAt = saved["at"]
            session.lastPercent = saved["lastPercent"]
        else:
            self.countOpenSession(db, mouseId, session, readingCount)
        # an earlier run's start has no boot clock reading, so it goes on this run's as long ago as the wall clock says, or, when the wall clock is behind the session's own last reading, as long as its saved readings span
        if (elapsed is not None):
            session.startElapsed = elapsed - max(0, max(now, session.lastAt) - startAt)
        return session

    def countOpenSession(self, db, mouseId, session, readingCount):
        # the session's readings are the last readingCount saved, which leaves out one the session before it saved in the same second, and its powers are counted in order so a tie goes to the power seen first, as it would have without the restart
        counted = db.execute("select power, percent from readings where mouseId = ? and at >= ? and origin in (0, 1, 5) order by at desc, seq desc limit ?", (mouseId, session.startAt, readingCount)).fetchall()
        session.powers = Counter(power for power, percent in reversed(counted) if power is not None)
        session.lowest = min((percent for power, percent in counted if percent is not None), default=None)
        # a mouse that reports only voltage or a rough level has no percent, so the last time comes from any reading
        lastAt = db.execute("select at from readings where mouseId = ? and at >= ? and origin in (0, 1, 5) order by at desc, seq desc limit 1", (mouseId, session.startAt)).fetchone()
        if (lastAt is not None):
            session.lastAt = lastAt[0]
        lastPercent = db.execute("select percent from readings where mouseId = ? and at >= ? and percent is not null order by at desc, seq desc limit 1", (mouseId, session.startAt)).fetchone()
        if (lastPercent is not None):
            session.lastPercent = lastPercent[0]

    def write(self, entry, row):
        saving = self.ensureLoaded(entry)
        # loading can find the key saved as HID++, and a UPower reading already waiting doesn't belong in that history
        if (row.origin == ORIGIN_UPOWER and entry.source == SOURCE_HIDPP):
            return
        if (saving and not self.takeBudget(entry)):
            # a row left out is handled like a failed write, so the next one saved follows a gap and nothing across it counts as whole
            entry.needsGap = True
            self.store.say("Mouse " + entry.key + " sent more changes than the history saves, so some were left out", FAILED_BUDGET + entry.key)
            return
        if (saving):
            try:
                with self.store.transaction() as db:
                    writer = DbWriter(db, entry.mouseId)
                    if (entry.nameChanged):
                        db.execute("update mice set name = ? where id = ?", (entry.name, entry.mouseId))
                    self.writeFlags(db, entry)
                    track = entry.track.copy()
                    if (entry.needsGap and row.origin != ORIGIN_GAP):
                        track.applyMarker(Row(row.at, ORIGIN_GAP, elapsed=row.elapsed), writer)
                    self.apply(track, row, writer)
                    writer.saveTrack(track, row)
            except sqlite3.Error as error:
                # the rollback undid the database, so the row goes to memory as it would with the history closed, and a newer reading is compared with it instead of with a level the mouse already left
                self.writeFailed(entry, error)
                self.keepInMemory(entry, row)
                return
            entry.needsGap = False
            entry.nameChanged = False
            entry.pendingFlags = {}
            entry.unsaved = None
            entry.retryAt = None
            entry.track = track
            if (entry.runTracked):
                entry.runSince = row.at if entry.runSince is None else min(entry.runSince, row.at)
                entry.runTracked = entry.runAfter is None or entry.runSince > entry.runAfter
            self.revisions += 1
            entry.revision = self.revisions
            # only a budget back to full counts as over, so a slow flood that keeps nibbling at it can't fill the journal with pairs of lines
            if (entry.budget >= WRITE_BURST - 1):
                self.store.works(FAILED_BUDGET + entry.key, "Mouse " + entry.key + "'s changes are saved again")
            self.noteWrite(entry, row)
            self.changed.emit(entry.key)
            return
        self.keepInMemory(entry, row)

    def keepInMemory(self, entry, row):
        if (entry.persist and entry.loaded):
            # the database went away under a loaded mouse, or didn't take its row, so it's loaded again once it's back, after a gap
            self.unload(entry)
        track = entry.track.copy()
        self.apply(track, row, NullWriter())
        flagChanged = (track.reportsItselfSince is None) != (entry.track.reportsItselfSince is None)
        if (flagChanged and entry.persist):
            entry.pendingFlags["reportsItselfSince"] = track.reportsItselfSince
        if (entry.persist):
            entry.unsaved = row
        entry.track = track
        self.noteWrite(entry, row)
        # nothing was saved, so the revision stays, but the service still has to pick up the new flag
        if (flagChanged):
            self.changed.emit(entry.key)

    def takeNewMouse(self):
        now = self.monotonic()
        if (self.newMiceAt is not None):
            self.newMice = min(NEW_MICE_BURST, self.newMice + (now - self.newMiceAt) / NEW_MICE_REFILL_SECONDS)
        self.newMiceAt = now
        if (self.newMice < 1):
            return False
        self.newMice -= 1
        return True

    def takeBudget(self, entry):
        now = self.monotonic()
        if (entry.budgetAt is not None):
            entry.budget = min(WRITE_BURST, entry.budget + (now - entry.budgetAt) / WRITE_REFILL_SECONDS)
        entry.budgetAt = now
        if (entry.budget < 1):
            return False
        entry.budget -= 1
        return True

    def noteWrite(self, entry, row):
        # a wake set's reading or a search's reading can have come in before the row written last, and the hold still counts from the latest write
        if (entry.lastWriteAt is None or row.arrived > entry.lastWriteAt):
            entry.lastWriteAt = row.arrived

    def apply(self, track, row, writer):
        if (row.origin in MARKER_ORIGINS):
            track.applyMarker(row, writer)
        else:
            track.applyReading(row, writer)

    def writeFailed(self, entry, error):
        entry.needsGap = True
        entry.retryAt = self.monotonic() + OPEN_RETRY_SECONDS
        if (isCorruption(error)):
            self.markCorrupt(error)
            self.unload(entry)
            return
        self.store.say("Couldn't save to the history: " + describe(error), FAILED_SAVE)
        self.schedule()


class HistoryUnavailable(Exception):
    pass


def drainBucket(percent):
    return (percent + 9) // 10 * 10 if 21 <= percent <= 80 else None


def chargeBucket(percent):
    return percent // 10 * 10 if 20 <= percent <= 79 else None


def monthOf(at):
    moment = time.gmtime(at)
    return "%04d-%02d" % (moment.tm_year, moment.tm_mon)


def bucketEntries(rows, bucketOf, buckets):
    # one figure per session and bucket, the median of its steps there, kept in the order the sessions came
    perBucket = {bucket: {} for bucket in buckets}
    for sessionId, fromPercent, minutes in rows:
        bucket = bucketOf(fromPercent)
        if (bucket is None):
            continue
        perBucket[bucket].setdefault(sessionId, []).append(minutes)
    return [bucketEntry(bucket, [statistics.median(values) for values in perBucket[bucket].values() if len(values) >= SESSION_STEPS]) for bucket in buckets]


def bucketEntry(bucket, figures):
    # the baseline is the first 20 sessions, and the three windows of 20 end at the newest session and the two before it, so they overlap each other but never the baseline
    first = statistics.median(figures[:BASELINE_SESSIONS]) if len(figures) >= BASELINE_SESSIONS else None
    last = None
    if (len(figures) >= BASELINE_SESSIONS + WINDOW_SESSIONS + WINDOWS - 1):
        last = [statistics.median(figures[len(figures) - WINDOW_SESSIONS - back:len(figures) - back]) for back in range(WINDOWS)]
    return {"bucket": bucket, "sessions": len(figures), "first": first, "last": last}


def emptyHistory(key):
    return {"version": HISTORY_VERSION, "key": key, "name": None, "source": None, "firstSeen": None, "reportsItself": False, "wakeSupport": False, "wakesVisible": True, "open": None, "drain": {"buckets": bucketEntries([], drainBucket, DRAIN_BUCKETS), "largeSteps": False}, "charge": [], "lastCharge": None, "lastError": None, "monthly": [], "sessionColumns": list(SESSION_COLUMNS), "sessions": [], "recent": []}


def readSummary(db, entry, key, now):
    found = db.execute("select id, name, source, firstSeen, reportsItselfSince, wakeSupport from mice where key = ?", (key,)).fetchone()
    if (found is None):
        return emptyHistory(key)
    mouseId, name, source, firstSeen, reportsItselfSince, wakeSupport = found
    summary = emptyHistory(key)
    summary.update({"name": name, "source": source, "firstSeen": firstSeen, "reportsItself": reportsItselfSince is not None, "wakeSupport": bool(wakeSupport)})
    if (entry is not None and entry.loaded):
        # a flag change that couldn't be saved yet is in memory, and the next transaction that succeeds saves it
        summary["reportsItself"] = entry.track.reportsItselfSince is not None
        summary["wakeSupport"] = entry.track.wakeSupport
    elif (entry is not None):
        # a track that was never loaded holds defaults, so only the changes waiting to be saved go over the saved row
        if ("reportsItselfSince" in entry.pendingFlags):
            summary["reportsItself"] = entry.pendingFlags["reportsItselfSince"] is not None
        if ("wakeSupport" in entry.pendingFlags):
            summary["wakeSupport"] = bool(entry.pendingFlags["wakeSupport"])
    # a lookup that hasn't answered keeps the saved wakeSupport, while no drop can be measured until one does
    summary["wakesVisible"] = entry is None or not entry.track.wakesUnseen
    summary["open"] = openSummary(db, mouseId)
    drops = db.execute("select session, fromPercent, minutesPerPercent from steps where mouseId = ? and kind = 0 and unbroken = 1 and fromPercent <= 80 and toPercent >= 20 order by at, seq", (mouseId,)).fetchall()
    sizes = [moved for (moved,) in db.execute("select fromPercent - toPercent from steps where mouseId = ? and kind = 0 order by at desc, seq desc limit ?", (mouseId, LARGE_STEP_COUNT))]
    summary["drain"] = {"buckets": bucketEntries(drops, drainBucket, DRAIN_BUCKETS), "largeSteps": len(sizes) == LARGE_STEP_COUNT and all(moved >= LARGE_STEP for moved in sizes)}
    rises = db.execute("select sessions.power, steps.session, steps.fromPercent, steps.minutesPerPercent from steps join sessions on sessions.id = steps.session where steps.mouseId = ? and steps.kind = 1 and steps.unbroken = 1 and steps.fromPercent >= 20 and steps.toPercent <= 80 order by steps.at, steps.seq", (mouseId,)).fetchall()
    powers = []
    for power, sessionId, fromPercent, minutes in rises:
        if (power not in powers):
            powers.append(power)
    summary["charge"] = [{"power": power, "buckets": bucketEntries([(sessionId, fromPercent, minutes) for rowPower, sessionId, fromPercent, minutes in rises if rowPower == power], chargeBucket, CHARGE_BUCKETS)} for power in powers]
    # sessions are numbered in the order they were saved, which their dates lose once a reboot sets the clock back behind them
    lastCharge = db.execute("select " + ", ".join(SESSION_COLUMNS) + " from sessions where mouseId = ? and kind = 1 order by id desc limit 1", (mouseId,)).fetchone()
    summary["lastCharge"] = None if lastCharge is None else dict(zip(SESSION_COLUMNS, lastCharge))
    summary["lastError"] = db.execute("select max(at) from readings where mouseId = ? and charging = ?", (mouseId, hidpp.CHARGING_ERROR)).fetchone()[0]
    summary["monthly"] = monthlySummary(db, mouseId)
    sessions = db.execute("select " + ", ".join(SESSION_COLUMNS) + " from sessions where mouseId = ? and startAt >= ? order by startAt desc, id desc limit ?", (mouseId, int(now) - SESSION_DAYS * 24 * 60 * 60, SESSION_LIMIT)).fetchall()
    summary["sessions"] = [list(row) for row in reversed(sessions)]
    summary["recent"] = recentSlots(db, mouseId, int(now))
    return summary


def openSummary(db, mouseId):
    found = db.execute("select id, kind, startAt, startPercent from sessions where mouseId = ? and endAt is null order by id desc limit 1", (mouseId,)).fetchone()
    if (found is None):
        return None
    sessionId, kind, startAt, startPercent = found
    # time left and time to full use the open session's steps at any level, found by the session alone, since a reboot that sets the clock back dates some before its start
    values = [minutes for (minutes,) in db.execute("select minutesPerPercent from steps where mouseId = ? and kind = ? and session = ? and unbroken = 1", (mouseId, kind, sessionId))]
    return {"kind": kind, "startAt": startAt, "startPercent": startPercent, "median": statistics.median(values) if values else None, "count": len(values)}


def monthlySummary(db, mouseId):
    months = {}

    def month(key):
        return months.setdefault(key, {"month": key, "drainValues": [], "chargeValues": {}, "drained": 0, "sessions": 0})

    for at, minutes in db.execute("select at, minutesPerPercent from steps where mouseId = ? and kind = 0 and unbroken = 1 and fromPercent <= 80 and toPercent >= 20", (mouseId,)):
        month(monthOf(at))["drainValues"].append(minutes)
    for power, at, minutes in db.execute("select sessions.power, steps.at, steps.minutesPerPercent from steps join sessions on sessions.id = steps.session where steps.mouseId = ? and steps.kind = 1 and steps.unbroken = 1 and steps.fromPercent >= 20 and steps.toPercent <= 80", (mouseId,)):
        month(monthOf(at))["chargeValues"].setdefault(power, []).append(minutes)
    # drained points come from each discharge's own ends, so a rebound or a gap inside it doesn't change them
    for endAt, startPercent, endPercent in db.execute("select endAt, startPercent, endPercent from sessions where mouseId = ? and kind = 0 and endAt is not null and startPercent is not null and endPercent is not null", (mouseId,)):
        month(monthOf(endAt))["drained"] += max(0, startPercent - endPercent)
    for (startAt,) in db.execute("select startAt from sessions where mouseId = ?", (mouseId,)):
        month(monthOf(startAt))["sessions"] += 1
    result = []
    for key in sorted(months):
        values = months[key]
        result.append({"month": key, "drain": statistics.median(values["drainValues"]) if values["drainValues"] else None, "charge": [[power, statistics.median(minutes)] for power, minutes in values["chargeValues"].items()], "drained": values["drained"], "sessions": values["sessions"]})
    return result


def readLevels(db, key, rangeName, now):
    levels = {"version": LEVELS_VERSION, "key": key, "range": rangeName, "slots": []}
    found = db.execute("select id, firstSeen from mice where key = ?", (key,)).fetchone()
    if (found is None):
        return levels
    mouseId, firstSeen = found
    count, seconds = LEVEL_RANGES[rangeName]
    now = int(now)
    if (rangeName not in SPAN_RANGES):
        levels["slots"] = recentSlots(db, mouseId, now, count, seconds)
    elif (seconds is None):
        levels["slots"] = spanSlots(db, mouseId, monthEdges(firstSeen, now)[-count:])
    else:
        levels["slots"] = spanSlots(db, mouseId, [(now - (count - slot) * seconds, now - (count - slot - 1) * seconds) for slot in range(count)])
    return levels


def monthEdges(firstSeen, now):
    # local months, since the window labels them in local time, with the current one ending now
    moment = time.localtime(min(firstSeen, now))
    year, month = moment.tm_year, moment.tm_mon
    edges = []
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    while (start <= now):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
        edges.append((int(start), min(int(end), now)))
        start = end
    return edges


def spanSlots(db, mouseId, edges):
    if (not edges):
        return []
    before = db.execute("select origin, " + VALUE_SQL + " from readings where mouseId = ? and at < ? and origin in (0, 1, 4, 5) order by at desc, seq desc limit 1", (mouseId, edges[0][0])).fetchone()
    carried = None if before is None or before[0] == ORIGIN_GAP else before[1]
    slots = []
    for slot, (start, end) in enumerate(edges):
        # the newest slot ends at the query's own second, and a reading made during that second belongs in it
        limit = end + 1 if slot == len(edges) - 1 else end
        # each slot is a range scan on the primary key, so ten years of readings cost a few hundred indexed queries
        low, high = db.execute("select min(" + VALUE_SQL + "), max(" + VALUE_SQL + ") from readings where mouseId = ? and at >= ? and at < ? and origin in (0, 1, 5)", (mouseId, start, limit)).fetchone()
        last = db.execute("select origin, " + VALUE_SQL + " from readings where mouseId = ? and at >= ? and at < ? and origin in (0, 1, 4, 5) order by at desc, seq desc limit 1", (mouseId, start, limit)).fetchone()
        charges = db.execute("select count(*) from sessions where mouseId = ? and kind = ? and startAt >= ? and startAt < ?", (mouseId, CHARGE, start, limit)).fetchone()[0]
        values = [value for value in (low, high, carried) if value is not None]
        slots.append([end, min(values), max(values), charges] if values else [end, None, None, charges])
        if (last is not None):
            carried = None if last[0] == ORIGIN_GAP else last[1]
    return slots


def recentSlots(db, mouseId, now, count=SLOTS, seconds=SLOT_SECONDS):
    start = now - count * seconds
    # a gap before the window means nothing is known at its start, the same as a gap inside it
    before = db.execute("select origin, percent, level, millivolts from readings where mouseId = ? and at < ? and origin in (0, 1, 4, 5) order by at desc, seq desc limit 1", (mouseId, start)).fetchone()
    rows = db.execute("select at, origin, percent, level, millivolts from readings where mouseId = ? and at >= ? and at <= ? order by at, seq", (mouseId, start, now)).fetchall()
    charges = db.execute("select startAt, coalesce(endAt, ?) from sessions where mouseId = ? and kind = 1 and coalesce(endAt, ?) >= ?", (now, mouseId, now, start)).fetchall()
    level = None if before is None or before[0] == ORIGIN_GAP else before[1:]
    slots = []
    index = 0
    for slot in range(count):
        slotStart = start + slot * seconds
        slotEnd = slotStart + seconds
        # the newest slot ends at the query's own second, and a reading made during that second belongs in it
        limit = slotEnd + 1 if slot == count - 1 else slotEnd
        while (index < len(rows) and rows[index][0] < limit):
            at, origin, percent, levelName, millivolts = rows[index]
            if (origin == ORIGIN_GAP):
                # nothing is known after a gap until the next reading
                level = None
            elif (origin in READING_ORIGINS):
                level = (percent, levelName, millivolts)
            index += 1
        charged = any(chargeStart < limit and chargeEnd > slotStart for chargeStart, chargeEnd in charges)
        if (level is None):
            slots.append([slotEnd, None, None, None, charged])
        else:
            slots.append([slotEnd, level[0], level[1], level[2], charged])
    return slots

