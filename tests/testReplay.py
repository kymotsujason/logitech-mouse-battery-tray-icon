import io
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

import health
import hidpp
import history

app = QApplication.instance() or QApplication([])
KEY = "02bc524c"
START = 1800000000
DAY = 24 * 60 * 60
# made up rates in minutes per 1% by range, uneven the way a real battery is, since a real discharge hasn't been measured
CURVE = ((60, 6.0), (30, 8.0), (0, 5.0))
SESSIONS_A_YEAR = 183


def minutesAt(percent):
    for floor, minutes in CURVE:
        if (percent > floor):
            return minutes
    return CURVE[-1][1]


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Replay:
    # a mouse used in three hour blocks with a rest long enough to sleep every 25 minutes, charged on a pad from 10% to full
    def __init__(self, folder, rate=lambda day, session: 1.0):
        self.clock = Clock()
        self.recorder = history.Recorder(folder, self.clock, timers=False)
        self.recorder.setWakeSupport(KEY, True)
        self.rate = rate
        self.percent = 100
        self.sessions = 0
        self.awakeInBlock = 0.0
        self.sinceRest = 0.0

    def send(self, at, percent, charging=0, power=0):
        self.clock.now = int(at)
        self.recorder.addReading(KEY, hidpp.BatteryReading(percent, charging, power=power), history.ORIGIN_EVENT, name="PRO X3 SUPERSTRIKE", at=int(at))
        self.clock.now = int(at) + history.HOLD_SECONDS + history.WAKE_SET_SECONDS
        self.recorder.tick()

    def wake(self, at):
        self.clock.now = int(at)
        self.recorder.addMarker(KEY, history.ORIGIN_WAKE, at=int(at))
        self.send(at + 1, self.percent)

    def run(self, days):
        at = float(START)
        self.wake(at)
        while (at < START + days * DAY):
            factor = self.rate((at - START) / DAY, self.sessions)
            while (self.percent > 10):
                step = minutesAt(self.percent) * 60 * factor
                at += step
                self.awakeInBlock += step
                self.sinceRest += step
                self.percent -= 1
                self.send(at, self.percent)
                if (self.awakeInBlock >= 3 * 60 * 60):
                    # a two hour break between blocks, asleep the whole time
                    at += 2 * 60 * 60
                    self.awakeInBlock = 0.0
                    self.sinceRest = 0.0
                    self.wake(at)
                elif (self.sinceRest >= 25 * 60):
                    at += 6 * 60
                    self.sinceRest = 0.0
                    self.wake(at)
            self.sessions += 1
            at += 60
            self.send(at, self.percent, charging=hidpp.CHARGING_CHARGING, power=2)
            while (self.percent < 100):
                at += 24
                self.percent += 1
                self.send(at, self.percent, charging=hidpp.CHARGING_CHARGING, power=2)
            at += 60
            self.send(at, 100, charging=hidpp.CHARGING_FULL, power=2)
            at += 600
            self.send(at, 100, charging=0, power=0)
            self.wake(at + 1)
        self.recorder.stop()
        self.recorder.store.lastAttempt = None
        return self.recorder.summary(KEY)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.patch = mock.patch.object(sys, "stderr", io.StringIO())
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.folder)

    def lines(self, summary):
        return [line.kind for line in health.assess(summary, hidpp.BatteryReading(100, 0), START + 130 * DAY).lines]

    def testAMonthInItsStillLearningWithItsCount(self):
        summary = Replay(self.folder).run(30)
        result = health.assess(summary, hidpp.BatteryReading(100, 0), START + 30 * DAY)
        self.assertEqual([line.kind for line in result.lines], [health.LEARNING])
        self.assertTrue(result.lines[0].text.startswith("Still learning, 0 of 3"))

    def testTheSameUnevenCurveAllTheWayThroughIsDoingWell(self):
        summary = Replay(self.folder).run(120)
        self.assertGreaterEqual(health.readyCount(summary["drain"]["buckets"]), 3)
        self.assertEqual(self.lines(summary), [health.GOOD])

    def testTheSameCurveWithSessionNoiseIsStillDoingWell(self):
        noise = random.Random(20261009)
        factors = {}

        def rate(day, session):
            return factors.setdefault(session, noise.uniform(0.8, 1.2))
        self.assertEqual(self.lines(Replay(self.folder, rate).run(120)), [health.GOOD])

    def testATwentyPercentFasterDrainInTheLastMonthIsAWarning(self):
        summary = Replay(self.folder, lambda day, session: 0.8 if day >= 90 else 1.0).run(120)
        result = health.assess(summary, hidpp.BatteryReading(100, 0), START + 130 * DAY)
        self.assertEqual(result.lines[0].kind, health.DRAIN)
        self.assertIn("about 20% less", result.lines[0].text)

    def testChargesAlwaysFromTenToFullCompareAsUnchanged(self):
        summary = Replay(self.folder).run(120)
        self.assertEqual([entry["power"] for entry in summary["charge"]], [2])
        self.assertIsNotNone(health.comparison(summary["charge"][0]["buckets"]))
        self.assertNotIn(health.CHARGE_LINE_KIND, self.lines(summary))


class SimulationTests(unittest.TestCase):
    # one figure per session, with the noise shared by every bucket, which is the case where taking the median across buckets doesn't help
    def everWarns(self, figures):
        for count in range(history.BASELINE_SESSIONS + history.WINDOW_SESSIONS + history.WINDOWS - 1, len(figures) + 1):
            shown = figures[:count]
            buckets = [history.bucketEntry(top, [value * base for value in shown]) for top, base in ((80, 6.0), (70, 8.0), (60, 5.0))]
            if (health.drainLine(health.comparison(buckets)) is not None):
                return True
        return False

    def share(self, wear, seed):
        noise = random.Random(seed)
        warned = 0
        histories = 1000
        for history_ in range(histories):
            figures = [(1 - (1 - wear) * session / SESSIONS_A_YEAR) * noise.uniform(0.8, 1.2) for session in range(SESSIONS_A_YEAR)]
            warned += self.everWarns(figures)
        return warned / histories

    def testUnderOneInTwentyHealthyBatteriesEverWarnInAYear(self):
        self.assertLess(self.share(1.0, 1), 0.05)

    def testAThirtyPercentLossOverAYearWarnsNearlyEveryone(self):
        self.assertGreater(self.share(0.7, 2), 0.95)


if (__name__ == "__main__"):
    unittest.main()
