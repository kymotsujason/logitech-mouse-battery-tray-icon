import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QAbstractAnimation, QObject, QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtDBus import QDBusMessage
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

import glass
import health
import hidpp
import historyWindow
import history
import mice
import panelColor
import serviceClient
import serviceState
import settings
import tray
import upower
from serviceState import ServiceEntry, ServiceState

app = QApplication.instance() or QApplication([])
NOW = time.time()


def entry(key="02bc524c", name="PRO X3 SUPERSTRIKE", reading=hidpp.BatteryReading(62, 0), asleep=False, pathKey=False, revision=1, lastRead=NOW):
    return ServiceEntry(key, pathKey, name, "/dev/hidraw4", reading, asleep, lastRead, NOW, True, True, revision)


def reply(key):
    summary = history.emptyHistory(key)
    summary["wakeSupport"] = True
    summary["recent"] = [[NOW + 60, 62, None, None, False]]
    return summary


def levelsReply(key, rangeName):
    return {"version": 1, "key": key, "range": rangeName, "slots": [[NOW - (25 - index) * 7 * 24 * 60 * 60, 30, 90, 1] for index in range(26)]}


class FakeWatcher(QObject):
    serviceRestarted = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.asked = []
        self.answers = {}
        self.hold = False
        self.waiting = []
        self.levelAsks = []
        self.levelAnswers = {}
        self.holdLevels = False
        self.waitingLevels = []

    def getLevels(self, key, rangeName, onAnswer):
        self.levelAsks.append((key, rangeName))
        if (self.holdLevels):
            self.waitingLevels.append((key, rangeName, onAnswer))
            return
        levels, error = self.levelAnswers.get((key, rangeName), (levelsReply(key, rangeName), None))
        onAnswer(levels, error)

    def getHistory(self, key, onAnswer):
        self.asked.append(key)
        if (self.hold):
            self.waiting.append((key, onAnswer))
            return
        summary, error = self.answers.get(key, (reply(key), None))
        onAnswer(summary, error)


class FakeUPower(QObject):
    mouseChanged = pyqtSignal(object)
    mouseRemoved = pyqtSignal(str)


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.watcher = FakeWatcher()
        self.upower = FakeUPower()
        self.mouseList = mice.MouseList()
        self.window = historyWindow.HistoryWindow(self.mouseList, self.watcher, self.upower, self.folder, None, tray.mouseLine, tray.iconStateFor)

    def tearDown(self):
        self.window.close()
        # freed here instead of by the cyclic GC, which would free them all in one pass at an unknown moment
        sip.delete(self.window)
        self.mouseList.stop()
        sip.delete(self.mouseList)
        shutil.rmtree(self.folder)

    def state(self, *entries):
        self.mouseList.applyService(ServiceState(False, False, ("/dev/hidraw4",), (), tuple(entries)))

    def opened(self, *entries):
        self.state(*entries)
        self.window.open()
        return self.window


class WindowTests(WindowCase):
    def testAnOpenWindowShowsTheMouseAndItsVerdictLine(self):
        window = self.opened(entry())
        self.assertEqual((window.nameLabel.text(), window.levelLabel.text(), window.statusLabel.text()), ("PRO X3 SUPERSTRIKE", "62%", "In use"))
        self.assertEqual(window.lineLabels[0].text(), "Still learning, 0 of 3 battery ranges measured.")

    def testItShowsLoadingUntilTheReplyComes(self):
        self.watcher.hold = True
        window = self.opened(entry())
        self.assertEqual((window.message.isVisibleTo(window), window.message.text()), (True, historyWindow.LOADING_TEXT))
        self.assertTrue(window.popup.isVisibleTo(window))
        key, onAnswer = self.watcher.waiting.pop()
        onAnswer(reply(key), None)
        self.assertFalse(window.message.isVisibleTo(window))

    def testAFailedFirstFetchSaysWhyAndARetryWaits(self):
        self.watcher.answers["02bc524c"] = (None, serviceState.HISTORY_UNAVAILABLE)
        window = self.opened(entry())
        self.assertEqual(window.message.text(), historyWindow.UNAVAILABLE_TEXT)
        self.assertTrue(window.retryTimer.isActive())

    def testA111ServiceAsksForARestart(self):
        for error in (serviceState.UNKNOWN_METHOD, serviceState.ACCESS_DENIED):
            self.window.close()
            self.window.replies.clear()
            self.window.errors.clear()
            self.window.lastFetch.clear()
            self.watcher.answers["02bc524c"] = (None, error)
            window = self.opened(entry())
            self.assertEqual(window.message.text(), historyWindow.RESTART_TEXT, error)

    def testALostServiceSaysSoInPlaceOfTheHistory(self):
        window = self.opened(entry())
        self.mouseList.applyService(None)
        self.assertEqual(window.message.text(), historyWindow.NO_SERVICE_TEXT)

    def testAPathKeyMouseSaysItsHistoryCantBeKept(self):
        window = self.opened(entry(key="/dev/hidraw4#1", pathKey=True))
        self.assertEqual(window.message.text(), historyWindow.NO_SERIAL_TEXT)
        self.assertEqual(self.watcher.asked, [])

    def testNoMouseShowsTheTraysStatusLine(self):
        window = self.opened()
        self.assertEqual((window.paneMessage.isVisibleTo(window), window.paneMessage.text()), (True, mice.STATUS_TEXT[mice.STATUS_WAITING]))

    def testRoughVoltageAndNoReadingMice(self):
        window = self.opened(entry(reading=hidpp.BatteryReading(None, 0, level="good")))
        self.assertEqual(window.levelLabel.text(), "Good")
        self.state(entry(reading=hidpp.BatteryReading(None, 0, millivolts=3850)))
        self.assertEqual(window.levelLabel.text(), "3850 mV")
        self.state(entry(reading=None))
        self.assertEqual((window.levelLabel.text(), window.statusLabel.text()), ("", "No reading yet"))

    def testTheStatusWordsFollowTheState(self):
        window = self.opened(entry(reading=hidpp.BatteryReading(45, hidpp.CHARGING_CHARGING)))
        self.assertEqual(window.statusLabel.text(), "Charging")
        # built now, since NOW is from when the suite loaded and a suite that crosses local midnight would put it on another day
        self.state(entry(asleep=True, lastRead=time.time()))
        self.assertTrue(window.statusLabel.text().startswith("Asleep, read at "))

    def testTheSidebarComesWithTwoMiceAndGoesWithOne(self):
        window = self.opened(entry())
        self.assertFalse(window.sidebar.isVisibleTo(window))
        self.assertEqual(window.width(), historyWindow.PANE_WIDTH)
        self.state(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        self.assertTrue(window.sidebar.isVisibleTo(window))
        self.assertEqual(window.width(), historyWindow.PANE_WIDTH + historyWindow.SIDEBAR_WIDTH)
        self.assertEqual([row[2] for row in window.sidebar.rows], ["62%", "18%"])
        self.state(entry())
        self.assertFalse(window.sidebar.isVisibleTo(window))
        self.assertEqual(window.width(), historyWindow.PANE_WIDTH)

    def testChoosingAMouseShowsLoadingUntilItsReply(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"))
        self.watcher.hold = True
        window.pick("11111111")
        self.assertEqual(window.message.text(), historyWindow.LOADING_TEXT)
        self.assertEqual(window.nameLabel.text(), "MX MASTER 3S")

    def testGoingBackToAMouseFetchesOnlyAfterItsRevisionChanged(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"))
        window.pick("11111111")
        window.pick("02bc524c")
        self.assertEqual(self.watcher.asked, ["02bc524c", "11111111"])
        window.lastFetch.clear()
        self.state(entry(revision=2), entry(key="11111111", name="MX Master 3S"))
        self.assertEqual(self.watcher.asked, ["02bc524c", "11111111", "02bc524c"])

    def testAFetchWaitsOutTheThirtySecondGap(self):
        window = self.opened(entry())
        self.state(entry(revision=2))
        self.assertEqual(self.watcher.asked, ["02bc524c"])
        self.assertTrue(window.fetchTimer.isActive())

    def upowerChange(self, key="02bc524c", reading=hidpp.BatteryReading(61, 0)):
        self.upower.mouseChanged.emit(upower.UPowerMouse("/org/freedesktop/UPower/devices/mouse_" + key, key, None, reading))

    def settle(self, window, key="02bc524c"):
        window.upowerChangedAt[key] -= historyWindow.UPOWER_SETTLE_SECONDS
        window.fetchTimer.stop()
        window.fetchTimer.timeout.emit()

    def testAUPowerChangeWaitsForTheServiceToSaveIt(self):
        # the service holds a reading for its wake set before it writes it, so a fetch at once would come back without it
        self.assertGreater(historyWindow.UPOWER_SETTLE_SECONDS, history.WAKE_SET_SECONDS + 1)
        window = self.opened(entry())
        window.lastFetch.clear()
        self.upowerChange()
        self.assertEqual(self.watcher.asked, ["02bc524c"])
        self.assertTrue(window.fetchTimer.isActive())
        self.assertGreater(window.fetchTimer.remainingTime(), (historyWindow.UPOWER_SETTLE_SECONDS - 1) * 1000)
        self.settle(window)
        self.assertEqual(self.watcher.asked, ["02bc524c", "02bc524c"])
        window.lastFetch.clear()
        window.refresh()
        self.assertEqual(len(self.watcher.asked), 2)

    def testAReplyAskedForDuringTheServicesHoldIsOld(self):
        # a change of the percent alone soon after the last write is held for up to HOLD_SECONDS before it's written
        window = self.opened(entry())
        self.upowerChange()
        mouse = window.selectedMouse()
        askedAt = window.upowerChangedAt["02bc524c"] + history.WAKE_SET_SECONDS + history.HOLD_SECONDS
        self.assertTrue(window.isOld(mouse, historyWindow.Reply(reply("02bc524c"), mouse.historyRevision, askedAt)))
        self.assertGreater(historyWindow.UPOWER_SETTLE_SECONDS, history.WAKE_SET_SECONDS + history.HOLD_SECONDS)

    def testAUPowerRefreshThatLeavesTheReadingAloneIsntAChange(self):
        window = self.opened(entry())
        self.upowerChange()
        changedAt = window.upowerChangedAt["02bc524c"]
        self.upowerChange()
        self.assertEqual(window.upowerChangedAt["02bc524c"], changedAt)
        self.upowerChange(reading=hidpp.BatteryReading(60, 0))
        self.assertGreater(window.upowerChangedAt["02bc524c"], changedAt)

    def testAUPowerMouseThatCameBackIsAChangeEvenWithTheSameReading(self):
        # the service writes a gap when the mouse goes and the first reading once it's back
        window = self.opened(entry())
        self.upowerChange()
        changedAt = window.upowerChangedAt["02bc524c"]
        self.upower.mouseRemoved.emit("/org/freedesktop/UPower/devices/mouse_02bc524c")
        self.upowerChange()
        self.assertGreater(window.upowerChangedAt["02bc524c"], changedAt)

    def testAUPowerChangeDuringAFetchMakesItsAnswerOld(self):
        self.watcher.hold = True
        window = self.opened(entry())
        self.upowerChange()
        key, onAnswer = self.watcher.waiting.pop()
        onAnswer(reply(key), None)
        self.assertFalse(window.message.isVisibleTo(window))
        self.watcher.hold = False
        window.lastFetch.clear()
        self.settle(window)
        self.assertEqual(self.watcher.asked, ["02bc524c", "02bc524c"])

    def testTheShownMousesShorterWaitTakesTheTimer(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"))
        window.pick("11111111")
        window.pick("02bc524c")
        self.state(entry(revision=2), entry(key="11111111", name="MX Master 3S", revision=2))
        self.assertGreater(window.fetchTimer.remainingTime(), 25 * 1000)
        window.lastFetch["11111111"] -= historyWindow.FETCH_GAP_SECONDS - 5
        window.pick("11111111")
        # a coarse Qt timer may fire up to 5% late, which remainingTime counts
        self.assertLess(window.fetchTimer.remainingTime(), 10 * 1000)

    def testAnErrorThatWontChangeIsntAskedForAgain(self):
        for error in historyWindow.FINAL_ERRORS:
            self.window.close()
            self.window.replies.clear()
            self.window.lastFetch.clear()
            self.watcher.asked.clear()
            self.watcher.answers["02bc524c"] = (None, error)
            window = self.opened(entry())
            self.assertFalse(window.fetchTimer.isActive(), error)
            window.lastFetch.clear()
            window.refresh()
            self.assertEqual(self.watcher.asked, ["02bc524c"], error)
            # a reopen is a new ask from the user, so it gets one more try
            window.close()
            window.lastFetch.clear()
            window.open()
            self.assertEqual(self.watcher.asked, ["02bc524c"] * 2, error)
            del self.watcher.answers["02bc524c"]
            window.lastFetch.clear()
            self.watcher.serviceRestarted.emit()
            self.assertEqual(self.watcher.asked, ["02bc524c"] * 3, error)

    def testAnErrorThatWontChangeStopsTheRefreshesToo(self):
        window = self.opened(entry())
        self.watcher.answers["02bc524c"] = (None, serviceClient.UNDECODABLE)
        window.lastFetch.clear()
        self.state(entry(revision=2))
        self.assertEqual((len(self.watcher.asked), window.refreshLabel.text()), (2, historyWindow.NOT_REFRESHED_TEXT))
        window.lastFetch.clear()
        window.refresh()
        self.assertEqual(len(self.watcher.asked), 2)
        self.assertFalse(window.fetchTimer.isActive())

    def testAReopenWithinTheGapShowsTheKeptReply(self):
        window = self.opened(entry())
        window.close()
        window.open()
        self.assertFalse(window.message.isVisibleTo(window))
        self.assertEqual(self.watcher.asked, ["02bc524c"])

    def testAReopenFetchesAReplyThatWentOldWhileClosed(self):
        window = self.opened(entry())
        window.close()
        self.state(entry(revision=2))
        window.lastFetch.clear()
        window.open()
        self.assertEqual(self.watcher.asked, ["02bc524c"] * 2)

    def testAHistoryAnswerThatLandsWhileClosedIsKept(self):
        self.watcher.hold = True
        window = self.opened(entry())
        window.close()
        key, onAnswer = self.watcher.waiting.pop()
        onAnswer(reply(key), None)
        window.lastFetch.clear()
        window.open()
        self.assertFalse(window.message.isVisibleTo(window))
        self.assertEqual(self.watcher.asked, ["02bc524c"])

    def testAnErrorThatLandsWhileClosedLeavesTheNextOpenItsTry(self):
        self.watcher.hold = True
        window = self.opened(entry())
        window.close()
        key, onAnswer = self.watcher.waiting.pop()
        onAnswer(None, serviceClient.UNDECODABLE)
        self.assertEqual(window.errors, {})
        self.watcher.hold = False
        window.lastFetch.clear()
        window.open()
        self.assertEqual(self.watcher.asked, ["02bc524c"] * 2)
        self.assertFalse(window.message.isVisibleTo(window))

    def testARangeErrorThatLandsWhileClosedLeavesTheNextOpenItsTry(self):
        window = self.opened(entry())
        self.watcher.holdLevels = True
        window.pickRange("year")
        window.close()
        key, rangeName, onAnswer = self.watcher.waitingLevels.pop()
        onAnswer(None, serviceState.READ_ALL_LIMITED)
        self.assertEqual((window.levelErrors, window.levelRetryTimer.isActive()), ({}, False))
        self.watcher.holdLevels = False
        window.lastFetch.clear()
        window.open()
        self.assertEqual(self.watcher.levelAsks, [("02bc524c", "year")] * 2)
        self.assertEqual(window.levelChart.slots, levelsReply("02bc524c", "year")["slots"])

    def testARangeAnswerThatLandsWhileClosedIsKept(self):
        window = self.opened(entry())
        self.watcher.holdLevels = True
        window.pickRange("year")
        window.close()
        key, rangeName, onAnswer = self.watcher.waitingLevels.pop()
        onAnswer(levelsReply(key, rangeName), None)
        window.lastFetch.clear()
        window.open()
        self.assertEqual(self.watcher.levelAsks, [("02bc524c", "year")])
        self.assertEqual(window.levelChart.slots, levelsReply("02bc524c", "year")["slots"])

    def testAReopenDoesntAskAgainForARangeOnItsWay(self):
        window = self.opened(entry())
        self.watcher.holdLevels = True
        window.pickRange("year")
        window.close()
        window.lastFetch.clear()
        window.open()
        self.assertEqual(self.watcher.levelAsks, [("02bc524c", "year")])

    def testAServiceRestartForgetsTheReplies(self):
        window = self.opened(entry())
        self.watcher.serviceRestarted.emit()
        # the revisions started over, so the reply is gone and the fetch for it waits out the gap
        self.assertEqual((window.replies, window.message.text()), ({}, historyWindow.LOADING_TEXT))
        self.assertTrue(window.fetchTimer.isActive())
        window.lastFetch.clear()
        self.watcher.serviceRestarted.emit()
        self.assertEqual(len(self.watcher.asked), 2)

    def testAStateChangeWhileClosedKeepsTheSidebarsWidth(self):
        two = (entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        window = self.opened(*two)
        window.close()
        self.state(*two)
        self.assertEqual(window.width(), historyWindow.PANE_WIDTH + historyWindow.SIDEBAR_WIDTH)

    def testOpeningAMinimizedWindowBringsItBack(self):
        window = self.opened(entry())
        window.showMinimized()
        self.assertTrue(window.isMinimized())
        window.open()
        self.assertEqual((window.isMinimized(), window.isVisible()), (False, True))

    def testAReopenedWindowStartsOnTheTraysMouse(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        shown = self.mouseList.shownMouse().key
        other = "11111111" if shown == "02bc524c" else "02bc524c"
        window.pick(other)
        window.close()
        window.open()
        self.assertEqual(window.selected, shown)

    def testTheFadesAreOnlyOnWhileTheyRun(self):
        # an opacity effect draws its widget from a cached image, so the chart's fade inside an idle pane fade never showed until the window lost focus
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        window.motionFactor = 1.0
        self.assertFalse(window.paneFade.isEnabled())
        window.showChart(1)
        self.assertTrue(window.chartFade.isEnabled())
        window.chartAnimation.setCurrentTime(window.chartAnimation.duration())
        self.assertFalse(window.chartFade.isEnabled())
        window.pick("11111111")
        self.assertTrue(window.paneFade.isEnabled())
        window.paneAnimation.setCurrentTime(window.paneAnimation.duration())
        self.assertFalse(window.paneFade.isEnabled())

    def testTurningMotionOffMidFadeLeavesTheFadeOff(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        window.motionFactor = 1.0
        window.showChart(1)
        window.pick("11111111")
        window.motionFactor = 0.0
        window.showChart(2)
        window.pick("02bc524c")
        self.assertEqual((window.chartFade.isEnabled(), window.paneFade.isEnabled()), (False, False))

    def testTheRangeControlShowsOnlyWithTheLevelChart(self):
        window = self.opened(entry())
        self.assertTrue(window.rangeControl.isVisibleTo(window))
        window.segments.choose(1, animate=False)
        self.assertFalse(window.rangeControl.isVisibleTo(window))
        window.segments.choose(0, animate=False)
        self.assertTrue(window.rangeControl.isVisibleTo(window))

    def testALabelNeverReadsTheMouseNameAsRichText(self):
        window = self.opened(entry(name="<b>X</b>"))
        self.assertEqual(window.nameLabel.text(), "<B>X</B>")
        labels = window.findChildren(historyWindow.QLabel)
        self.assertTrue(labels)
        for each in labels:
            self.assertEqual(each.textFormat(), Qt.TextFormat.PlainText)

    def testAWaitingNotRefreshedLineKeepsTheOldLines(self):
        window = self.opened(entry())
        before = [each.text() for each in window.lineLabels]
        self.watcher.answers["02bc524c"] = (None, serviceState.HISTORY_UNAVAILABLE)
        window.lastFetch.clear()
        self.state(entry(revision=2))
        self.assertEqual(len(self.watcher.asked), 2)
        self.assertEqual(window.refreshLabel.text(), historyWindow.NOT_REFRESHED_TEXT)
        self.assertEqual([each.text() for each in window.lineLabels], before)
        self.assertFalse(window.message.isVisibleTo(window))
        del self.watcher.answers["02bc524c"]
        window.lastFetch.clear()
        self.state(entry(revision=3))
        self.assertEqual(window.refreshLabel.text(), "")

    def testAReplyOlderThanTwoHoursIsAskedForAgain(self):
        old = reply("02bc524c")
        old["recent"] = [[time.time() - 3 * 60 * 60, 62, None, None, False]]
        self.watcher.answers["02bc524c"] = (old, None)
        window = self.opened(entry())
        self.assertEqual(self.watcher.asked, ["02bc524c"])
        self.assertFalse(window.ageTimer.isActive())
        self.assertTrue(window.fetchTimer.isActive())
        window.lastFetch.clear()
        fresh = reply("02bc524c")
        # built now, since NOW is from when the suite loaded and the cap check fails once it is two minutes old
        fresh["recent"] = [[time.time() + 60, 62, None, None, False]]
        self.watcher.answers["02bc524c"] = (fresh, None)
        window.refresh()
        self.assertEqual(self.watcher.asked, ["02bc524c"] * 2)
        # the fresh reply waits for its own two hours, and not a second less than the clock allows
        self.assertTrue(window.ageTimer.isActive())
        self.assertGreater(window.ageTimer.interval(), (historyWindow.STALE_SLOT_SECONDS - 60) * 1000)
        self.assertLessEqual(window.ageTimer.interval(), historyWindow.STALE_SLOT_SECONDS * 1000)
        window.lastFetch.clear()
        window.refresh()
        self.assertEqual(len(self.watcher.asked), 2)

    def testThePortalsDarkChoiceWinsOverALightPalette(self):
        saved = QPalette(app.palette())
        light = QPalette(saved)
        light.setColor(QPalette.ColorRole.Window, QColor("#F6F6F6"))
        app.setPalette(light)
        try:
            window = self.opened(entry())
            window.setPortal("color-scheme", 0)
            self.assertIs(window.theme, glass.LIGHT)
            window.setPortal("color-scheme", panelColor.PREFER_DARK)
            self.assertIs(window.theme, glass.DARK)
        finally:
            app.setPalette(saved)

    def portalSignal(self, key, value):
        message = QDBusMessage.createSignal("/org/freedesktop/portal/desktop", panelColor.SETTINGS_INTERFACE, "SettingChanged")
        message.setArguments([panelColor.APPEARANCE, key, value])
        return message

    def testTheReducedMotionChoiceTurnsTheFadesOff(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"))
        self.assertEqual(window.motionFactor, 1.0)
        window.onPortalSetting(self.portalSignal("reduced-motion", 1))
        self.assertEqual(window.motionFactor, 0.0)
        window.pick("11111111")
        self.assertEqual((window.paneAnimation.state(), window.paneFade.isEnabled()), (QAbstractAnimation.State.Stopped, False))

    def testAKnownReducedMotionSurvivesAReopen(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"))
        window.setPortal("reduced-motion", 1)
        window.close()
        window.open()
        self.assertEqual(window.motionFactor, 0.0)
        window.pick("11111111")
        self.assertEqual(window.paneAnimation.state(), QAbstractAnimation.State.Stopped)
        window.setPortal("reduced-motion", 0)
        self.assertEqual(window.motionFactor, 1.0)

    def testTurningMotionOffFinishesWhatIsRunning(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        window.motionFactor = 1.0
        # the glow only fades once a first paint has made the backdrop
        window.grab()
        window.pick("11111111")
        window.segments.choose(2)
        window.setGlow(QColor("#FF0000"))
        running = (window.paneAnimation, window.chartAnimation, window.segments.animation, window.glowAnimation)
        for animation in running:
            self.assertEqual(animation.state(), QAbstractAnimation.State.Running)
        window.setPortal("reduced-motion", 1)
        for animation in running:
            self.assertEqual(animation.state(), QAbstractAnimation.State.Stopped)
        self.assertEqual((window.paneFade.opacity(), window.paneFade.isEnabled(), window.chartFade.isEnabled()), (1.0, False, False))
        self.assertEqual(window.segments.thumbX, window.segments.segmentX(2))
        self.assertEqual(window.glow, QColor("#FF0000"))

    def testAPickDuringAFadePicksUpWhereItIs(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S"), )
        window.motionFactor = 1.0
        window.pick("11111111")
        self.assertEqual(window.paneAnimation.startValue(), 0.0)
        window.paneAnimation.setCurrentTime(window.paneAnimation.duration() // 2)
        halfway = window.paneFade.opacity()
        self.assertGreater(halfway, 0.3)
        window.pick("02bc524c")
        self.assertAlmostEqual(window.paneFade.opacity(), halfway)
        self.assertAlmostEqual(window.paneAnimation.startValue(), halfway)
        self.assertLess(window.paneAnimation.duration(), historyWindow.PANE_FADE_MS)
        self.assertTrue(window.paneFade.isEnabled())
        self.assertEqual(window.paneAnimation.state(), QAbstractAnimation.State.Running)
        window.paneAnimation.setCurrentTime(window.paneAnimation.duration())
        window.pick("11111111")
        self.assertEqual(window.paneAnimation.startValue(), 0.0)
        self.assertEqual(window.paneAnimation.duration(), historyWindow.PANE_FADE_MS)

    def testAChartSwitchDuringAFadePicksUpWhereItIs(self):
        window = self.opened(entry())
        window.motionFactor = 1.0
        window.showChart(1)
        window.chartAnimation.setCurrentTime(window.chartAnimation.duration() // 2)
        halfway = window.chartFade.opacity()
        self.assertGreater(halfway, 0.3)
        window.showChart(2)
        self.assertAlmostEqual(window.chartFade.opacity(), halfway)
        self.assertAlmostEqual(window.chartAnimation.startValue(), halfway)
        self.assertLess(window.chartAnimation.duration(), historyWindow.CHART_FADE_MS)

    def testTheRangeControlSitsInTheChartBoxsTopRightCorner(self):
        window = self.opened(entry())
        control = window.rangeControl.geometry()
        chart = window.chartRect
        self.assertTrue(chart.contains(QRectF(control)))
        self.assertLess(chart.right() - control.right(), 24)
        self.assertLess(control.top() - chart.top(), 12)

    def testTheWeekIsDrawnFromTheHistoryWithoutACall(self):
        window = self.opened(entry())
        self.assertEqual(self.watcher.levelAsks, [])
        self.assertEqual(window.levelChart.slots, reply("02bc524c")["recent"])

    def testAStateChangeKeepsTheLevelChartsReadout(self):
        window = self.opened(entry())
        QTest.mouseMove(window.levelChart, window.levelChart.area().center().toPoint())
        hovered = window.levelChart.hover
        self.assertIsNotNone(hovered)
        self.state(entry(reading=hidpp.BatteryReading(61, 0)))
        self.assertEqual(window.levelChart.hover, hovered)

    def testPickingARangeAsksForItOnceAndSavesTheChoice(self):
        window = self.opened(entry())
        saved = []
        window.levelRangeChanged.connect(saved.append)
        window.pickRange("year")
        self.assertEqual((self.watcher.levelAsks, saved, window.levelChart.title), ([("02bc524c", "year")], ["year"], "Battery Level, Last Year"))
        self.assertEqual(window.levelChart.slots, levelsReply("02bc524c", "year")["slots"])
        window.pickRange("week")
        window.pickRange("year")
        self.assertEqual(len(self.watcher.levelAsks), 1)
        self.assertEqual(window.rangeControl.index, historyWindow.RANGE_NAMES.index("year"))

    def testANewHistoryAsksForTheRangeAgain(self):
        window = self.opened(entry())
        window.pickRange("month")
        window.lastFetch.clear()
        self.state(entry(revision=2))
        self.assertEqual(self.watcher.levelAsks, [("02bc524c", "month")] * 2)

    def testALoadingRangeSaysSo(self):
        window = self.opened(entry())
        self.watcher.holdLevels = True
        window.pickRange("all")
        self.assertEqual((window.levelChart.title, window.levelChart.emptyText()), ("Battery Level, All Time", historyWindow.LOADING_TEXT))

    def testANewHistoryKeepsTheOldBarsWhileTheRangeReloads(self):
        window = self.opened(entry())
        window.pickRange("month")
        before = window.levelChart.slots
        self.watcher.holdLevels = True
        window.lastFetch.clear()
        self.state(entry(revision=2))
        self.assertEqual((window.levelChart.slots, window.levelChart.message), (before, None))
        self.assertEqual(self.watcher.levelAsks, [("02bc524c", "month")] * 2)

    def testARefusedReloadKeepsTheOldBars(self):
        window = self.opened(entry())
        window.pickRange("month")
        before = window.levelChart.slots
        self.watcher.levelAnswers[("02bc524c", "month")] = (None, serviceState.READ_ALL_LIMITED)
        window.lastFetch.clear()
        self.state(entry(revision=2))
        self.assertEqual((window.levelChart.slots, window.levelChart.message), (before, None))
        self.assertTrue(window.levelRetryTimer.isActive())

    def answerOnce(self, error):
        self.watcher.levelAnswers[("02bc524c", "year")] = (None, error)
        window = self.opened(entry())
        window.pickRange("year")
        return (window.levelChart.emptyText(), window.levelRetryTimer.isActive())

    def testAnUndecodableRangeIsntAskedForAgain(self):
        self.assertEqual(self.answerOnce(serviceClient.UNDECODABLE), (historyWindow.RANGE_ERROR_TEXT, False))

    def testARangeTheServiceDoesntKnowSaysToRestart(self):
        self.assertEqual(self.answerOnce(serviceState.INVALID_ARGS), (historyWindow.RANGE_RESTART_TEXT, False))

    def testTheCustomControlsHaveNamesForScreenReaders(self):
        window = self.opened(entry())
        window.popup.setSeconds(60)
        self.assertEqual(window.popup.accessibleName(), "Check whether mice are asleep, 1 min")
        self.assertEqual(window.choiceMenu.accessibleName(), "Check whether mice are asleep")
        self.assertEqual(window.rangeControl.accessibleName(), "Chart range, Last 7 Days")
        window.pickRange("year")
        self.assertEqual(window.rangeControl.accessibleName(), "Chart range, Last Year")
        self.assertEqual((window.segments.accessibleName(), window.levelChart.accessibleName(), window.hoursChart.accessibleName()), ("Chart, Level", "Battery Level, Last Year", "Hours per Charge, All Time"))
        window.segments.choose(1, animate=False)
        self.assertEqual(window.segments.accessibleName(), "Chart, Hours per Charge")
        self.state(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        window.pick("02bc524c")
        self.assertEqual(window.sidebar.accessibleName(), "Mice, PRO X3 SUPERSTRIKE, 62%")
        window.pick("11111111")
        self.assertEqual(window.sidebar.accessibleName(), "Mice, MX Master 3S, 18%")
        window.popup.setSeconds(30)
        QTest.mouseClick(window.popup, Qt.MouseButton.LeftButton)
        self.assertEqual(window.choiceMenu.accessibleDescription(), "Every 30 seconds")
        QTest.keyClick(window.choiceMenu, Qt.Key.Key_Down)
        self.assertEqual(window.choiceMenu.accessibleDescription(), "Every minute")

    def testAnOldServiceSaysToRestartForTheRange(self):
        self.watcher.levelAnswers[("02bc524c", "year")] = (None, serviceState.UNKNOWN_METHOD)
        window = self.opened(entry())
        window.pickRange("year")
        self.assertEqual(window.levelChart.emptyText(), historyWindow.RANGE_RESTART_TEXT)

    def testARefusedRangeIsAskedAgainSoon(self):
        self.watcher.levelAnswers[("02bc524c", "year")] = (None, serviceState.READ_ALL_LIMITED)
        window = self.opened(entry())
        window.pickRange("year")
        self.assertTrue(window.levelRetryTimer.isActive())
        self.assertEqual(window.levelChart.emptyText(), historyWindow.LOADING_TEXT)
        del self.watcher.levelAnswers[("02bc524c", "year")]
        window.levelRetryTimer.timeout.emit()
        self.assertEqual(window.levelChart.slots, levelsReply("02bc524c", "year")["slots"])

    def testTheWindowOpensOnTheSavedRange(self):
        window = historyWindow.HistoryWindow(self.mouseList, self.watcher, self.upower, self.folder, None, tray.mouseLine, tray.iconStateFor, levelRange="halfYear")
        try:
            self.state(entry())
            window.open()
            self.assertEqual((window.rangeControl.index, self.watcher.levelAsks), (historyWindow.RANGE_NAMES.index("halfYear"), [("02bc524c", "halfYear")]))
        finally:
            window.close()
            sip.delete(window)

    def testTheSettingButtonSendsTheChoiceAndShowsItShort(self):
        window = self.opened(entry())
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        window.popup.pick(30)
        self.assertEqual((picked, window.popup.text()), ([30], "30 s"))

    def openMenu(self, seconds=0):
        window = self.opened(entry())
        window.popup.setSeconds(seconds)
        QTest.mouseClick(window.popup, Qt.MouseButton.LeftButton)
        return window, window.choiceMenu

    def rowCenter(self, menu, index):
        return menu.rowRect(index).center().toPoint()

    def testTheSleepMenuOpensOverItsButtonWithOneChoiceMarked(self):
        window, menu = self.openMenu(60)
        self.assertTrue(menu.isVisibleTo(window))
        self.assertEqual(menu.marked, 2)
        # the marked row sits over the button, the way a macOS pop up button opens
        button = window.popup.mapTo(window, window.popup.rect().center())
        self.assertTrue(menu.rowRect(2).contains(QPointF(menu.rowRect(2).center().x(), button.y())))
        self.assertTrue(QRectF(window.rect()).contains(menu.panel))

    def buttonCenter(self, window):
        return window.popup.mapTo(window, window.popup.rect().center())

    def testADoubleClickOnTheSleepButtonPicksNothing(self):
        window = self.opened(entry())
        QTest.qWaitForWindowExposed(window)
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        for seconds in (0, 30):
            window.choiceMenu.hide()
            window.popup.setSeconds(seconds)
            # sent through the window, so the press opens the menu and the double click lands on whatever then covers the button
            QTest.mouseDClick(window.windowHandle(), Qt.MouseButton.LeftButton, pos=self.buttonCenter(window))
            self.assertEqual((picked, window.choiceMenu.isVisibleTo(window)), ([], True), seconds)

    def testASecondClickOnTheSleepButtonClosesTheMenuWithoutAPick(self):
        for seconds in (0, 30):
            window, menu = self.openMenu(seconds)
            picked = []
            window.sleepCheckChanged.connect(picked.append)
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.mapFrom(window, self.buttonCenter(window)))
            self.assertEqual((picked, menu.isVisibleTo(window)), ([], False), seconds)
            window.sleepCheckChanged.disconnect(picked.append)

    def testTheThreeSleepCheckTablesListTheSameChoices(self):
        self.assertEqual(tuple(seconds for seconds, name in historyWindow.SLEEP_CHOICES), settings.SLEEP_CHECK_CHOICES)
        self.assertEqual(tuple(historyWindow.SLEEP_SHORT), settings.SLEEP_CHECK_CHOICES)

    def testOnlyTheMarkedChoiceEverSitsUnderTheButton(self):
        for seconds, name in historyWindow.SLEEP_CHOICES:
            window, menu = self.openMenu(seconds)
            under = menu.rowAt(QPointF(menu.mapFrom(window, self.buttonCenter(window))))
            self.assertIn(under, (None, menu.marked), name)
            self.assertTrue(QRectF(menu.rect()).contains(menu.panel), name)
            QTest.keyClick(menu, Qt.Key.Key_Escape)

    def testClickingAChoicePicksOnlyThatOne(self):
        window, menu = self.openMenu()
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=self.rowCenter(menu, 3))
        self.assertEqual((picked, window.popup.text(), menu.isVisibleTo(window)), ([300], "5 min", False))

    def testTheKeysMoveAndPick(self):
        window, menu = self.openMenu(30)
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        QTest.keyClick(menu, Qt.Key.Key_Down)
        QTest.keyClick(menu, Qt.Key.Key_Return)
        self.assertEqual(picked, [60])

    def testEscapeAndAClickOutsideCloseItWithoutAPick(self):
        window, menu = self.openMenu()
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        self.assertFalse(menu.isVisibleTo(window))
        QTest.mouseClick(window.popup, Qt.MouseButton.LeftButton)
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
        self.assertEqual((menu.isVisibleTo(window), picked), (False, []))

    def testTheMenuIsMadeOnceAndClosesWithTheWindow(self):
        window, menu = self.openMenu()
        for count in range(4):
            QTest.keyClick(menu, Qt.Key.Key_Escape)
            QTest.mouseClick(window.popup, Qt.MouseButton.LeftButton)
        self.assertEqual((window.findChildren(QMenu), window.findChildren(historyWindow.ChoiceMenu)), ([], [menu]))
        window.close()
        self.assertFalse(menu.isVisibleTo(window))

    def testTheHealthBoxGrowsTheWindowUpTo640(self):
        window = self.opened(entry())
        summary = reply("02bc524c")
        summary["lastError"] = NOW
        summary["lastCharge"] = {"kind": 1, "startAt": NOW, "endAt": NOW, "startPercent": 20, "endPercent": 80, "power": 1, "readingCount": 10, "slowCount": 9, "errorCount": 1, "inferred": 0}
        summary["drain"]["buckets"] = [{"bucket": top, "sessions": 42, "first": 30.0, "last": [24.0, 24.0, 24.0]} for top in (80, 70, 60)] + [{"bucket": top, "sessions": 0, "first": None, "last": None} for top in (50, 40, 30)]
        window.replies["02bc524c"] = historyWindow.Reply(summary, 1, time.monotonic())
        window.refresh()
        self.assertEqual(len(window.health.lines), 2)
        self.assertGreater(window.height(), historyWindow.BASE_HEIGHT)
        self.assertLessEqual(window.height(), historyWindow.MAX_HEIGHT)


class FrostTests(unittest.TestCase):
    def testTheFrostIsMadeAgainOnlyOnAResizeOrAGlowChange(self):
        folder = tempfile.mkdtemp()
        mouseList = mice.MouseList()
        mouseList.applyService(ServiceState(False, False, ("/dev/hidraw4",), (), (entry(),)))
        window = historyWindow.HistoryWindow(mouseList, FakeWatcher(), None, folder, None, tray.mouseLine, tray.iconStateFor)
        try:
            with mock.patch.object(glass, "paintBackdrop", wraps=glass.paintBackdrop) as painted:
                window.open()
                window.grab()
                window.grab()
                self.assertEqual(painted.call_count, 1)
                window.resize(window.width() + 40, window.height())
                window.grab()
                self.assertEqual(painted.call_count, 2)
                window.motionFactor = 0.0
                mouseList.applyService(ServiceState(False, False, ("/dev/hidraw4",), (), (entry(reading=hidpp.BatteryReading(15, 0)),)))
                window.grab()
                self.assertEqual(painted.call_count, 3)
        finally:
            window.close()
            sip.delete(window)
            sip.delete(mouseList)
            shutil.rmtree(folder)



class DesktopBlurTests(unittest.TestCase):
    def testOutsideKdeTheBlurIsntAskedFor(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertFalse(historyWindow.enableDesktopBlur(QWidget()))

    def testAMissingLibraryLeavesThePaintedBackdrop(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), mock.patch.object(historyWindow.ctypes, "CDLL", side_effect=OSError("missing")):
            self.assertFalse(historyWindow.enableDesktopBlur(QWidget()))
    def testABlurKWinWontDrawIsntCountedAsAvailable(self):
        library = mock.Mock()
        getattr(library, historyWindow.AVAILABLE_SYMBOL).return_value = False
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), mock.patch.object(historyWindow.ctypes, "CDLL", return_value=library):
            self.assertFalse(historyWindow.desktopBlurAvailable())
        getattr(library, historyWindow.AVAILABLE_SYMBOL).assert_called_once_with(historyWindow.BLUR_BEHIND)

    def testOutsideKdeTheBlurIsntAvailable(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertFalse(historyWindow.desktopBlurAvailable())


class WindowBlurTests(WindowCase):
    def openWithBlur(self, answers):
        library = mock.Mock()
        getattr(library, historyWindow.AVAILABLE_SYMBOL).side_effect = answers
        intervals = []
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), mock.patch.object(historyWindow.ctypes, "CDLL", return_value=library):
            window = self.opened(entry())
            self.assertFalse(window.blurred)
            while (window.blurTimer.isActive()):
                intervals.append(window.blurTimer.interval())
                window.blurTimer.stop()
                window.blurTimer.timeout.emit()
        self.assertEqual(getattr(library, historyWindow.BLUR_SYMBOL).call_count, 1)
        return window, intervals

    def testTheBlurIsCheckedAgainOnceKWinHasAnswered(self):
        # KWin's blur manager is only bound after the event loop has run, so the check made as the window shows reads False
        window, intervals = self.openWithBlur([False, True])
        self.assertTrue(window.blurred)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertEqual(intervals, [historyWindow.BLUR_CHECK_MS[0]])

    def testTheBlurIsCheckedSoonAndOnceMoreLater(self):
        # KWin answered within 20 ms on the machine it was measured on, so the first check comes soon and a slower one still gets a second
        window, intervals = self.openWithBlur([False, False, True])
        self.assertTrue(window.blurred)
        self.assertEqual(intervals, list(historyWindow.BLUR_CHECK_MS))
        self.assertLessEqual(historyWindow.BLUR_CHECK_MS[0], 30)

    def testABlurThatNeverComesKeepsTheBackdropOpaque(self):
        window, intervals = self.openWithBlur([False, False, False])
        self.assertFalse(window.blurred)


if (__name__ == "__main__"):
    unittest.main()
