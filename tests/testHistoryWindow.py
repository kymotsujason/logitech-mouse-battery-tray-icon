import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

import glass
import health
import hidpp
import historyWindow
import history
import mice
import serviceState
import tray
from serviceState import ServiceEntry, ServiceState

app = QApplication.instance() or QApplication([])
NOW = time.time()


def entry(key="02bc524c", name="PRO X3 SUPERSTRIKE", reading=hidpp.BatteryReading(62, 0), asleep=False, pathKey=False, revision=1):
    return ServiceEntry(key, pathKey, name, "/dev/hidraw4", reading, asleep, NOW, NOW, True, True, revision)


def reply(key):
    summary = history.emptyHistory(key)
    summary["wakeSupport"] = True
    summary["recent"] = [[NOW + 60, 62, None, None, False]]
    return summary


class FakeWatcher(QObject):
    serviceRestarted = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.asked = []
        self.answers = {}
        self.hold = False
        self.waiting = []

    def getHistory(self, key, onAnswer):
        self.asked.append(key)
        if (self.hold):
            self.waiting.append((key, onAnswer))
            return
        summary, error = self.answers.get(key, (reply(key), None))
        onAnswer(summary, error)


class FakeUPower(QObject):
    mouseChanged = pyqtSignal(object)


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
        self.state(entry(asleep=True))
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

    def testAUPowerChangeMakesTheReplyOld(self):
        window = self.opened(entry())
        window.lastFetch.clear()

        class UPowerMouse:
            key = "02bc524c"
        self.upower.mouseChanged.emit(UPowerMouse())
        self.assertEqual(self.watcher.asked, ["02bc524c", "02bc524c"])

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

    def testAReopenedWindowStartsOnTheTraysMouse(self):
        window = self.opened(entry(), entry(key="11111111", name="MX Master 3S", reading=hidpp.BatteryReading(18, 0)))
        shown = self.mouseList.shownMouse().key
        other = "11111111" if shown == "02bc524c" else "02bc524c"
        window.pick(other)
        window.close()
        window.open()
        self.assertEqual(window.selected, shown)

    def testTheSettingButtonSendsTheChoiceAndShowsItShort(self):
        window = self.opened(entry())
        picked = []
        window.sleepCheckChanged.connect(picked.append)
        window.popup.pick(30)
        self.assertEqual((picked, window.popup.text()), ([30], "30 s"))

    def testTheHealthBoxGrowsTheWindowUpTo640(self):
        window = self.opened(entry())
        summary = reply("02bc524c")
        summary["lastError"] = NOW
        summary["lastCharge"] = {"kind": 1, "startAt": NOW, "endAt": NOW, "startPercent": 20, "endPercent": 80, "power": 1, "readingCount": 10, "slowCount": 9, "errorCount": 1, "inferred": 0}
        summary["drain"]["buckets"] = [{"bucket": top, "sessions": 42, "first": 30.0, "last": [24.0, 24.0, 24.0]} for top in (80, 70, 60)] + [{"bucket": top, "sessions": 0, "first": None, "last": None} for top in (50, 40, 30)]
        window.replies["02bc524c"] = historyWindow.Reply(summary, 1)
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
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), mock.patch.object(historyWindow.ctypes, "CDLL", return_value=library):
            window = self.opened(entry())
            self.assertFalse(window.blurred)
            self.assertTrue(window.blurTimer.isActive())
            window.blurTimer.stop()
            window.blurTimer.timeout.emit()
        self.assertEqual(getattr(library, historyWindow.BLUR_SYMBOL).call_count, 1)
        return window

    def testTheBlurIsCheckedAgainOnceKWinHasAnswered(self):
        # KWin's blur manager is only bound after the event loop has run, so the check made as the window shows reads False
        window = self.openWithBlur([False, True])
        self.assertTrue(window.blurred)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))

    def testABlurThatNeverComesKeepsTheBackdropOpaque(self):
        window = self.openWithBlur([False, False])
        self.assertFalse(window.blurred)


if (__name__ == "__main__"):
    unittest.main()
