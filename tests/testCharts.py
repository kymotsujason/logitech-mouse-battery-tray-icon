import calendar
import locale
import os
import re
import sys
import time
import unittest
import unittest.mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QAbstractAnimation, QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, QImage
from PyQt6.QtWidgets import QApplication

import charts
import glass
import history
import historyWindow

app = QApplication.instance() or QApplication([])
NOW = int(time.time())


def slots(percent=62, charged=False):
    return [[NOW - (83 - index) * 7200, percent, None, None, charged] for index in range(84)]


def rendered(chart):
    chart.resize(700, 238)
    image = QImage(chart.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    chart.render(image)
    return image


class ChartCase(unittest.TestCase):
    def setUp(self):
        self.made = []

    def tearDown(self):
        # freed here instead of by the cyclic GC, which would free them all in one pass at an unknown moment
        for chart in self.made:
            sip.delete(chart)

    def make(self, chart):
        self.made.append(chart)
        return chart


class LevelChartTests(ChartCase):
    def testAChartWithNoReadingsSaysSo(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[NOW, None, None, None, False]] * 84)
        seen = []
        # only the text is kept, since a mock that kept the painter would end it on a freed image once Python let it go
        with unittest.mock.patch.object(charts.LevelChart, "paintEmpty", lambda self, painter, text: seen.append(text)):
            rendered(chart)
        self.assertEqual(seen, [charts.LEVEL_EMPTY])

    def testBarsAreDrawn(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(slots())
        image = rendered(chart)
        area = chart.area()
        self.assertGreater(image.pixelColor(int(area.x() + 3), int(area.bottom() - 5)).alpha(), 0)

    def testARoughLevelHasAHeight(self):
        self.assertEqual(charts.slotValue([NOW, None, "good", None, False]), 70)
        self.assertIsNone(charts.slotValue([NOW, None, "unknown level", None, False]))

    def testHoveringABarPicksIt(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(slots())
        chart.resize(700, 238)
        area = chart.area()
        self.assertEqual(chart.barAt(area.x() + 1), 0)
        self.assertEqual(chart.barAt(area.right() - 1), 83)
        self.assertIsNone(chart.barAt(area.x() - 5))

    def testTheSameRangeKeepsTheHoveredBar(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(slots())
        chart.hover = 40
        chart.setSlots(slots())
        self.assertEqual(chart.hover, 40)
        chart.setSlots(slots(percent=61))
        self.assertEqual(chart.hover, 40)
        for change in ({"rangeName": "month"}, {"message": historyWindow.LOADING_TEXT}):
            chart.setSlots(slots())
            chart.hover = 40
            chart.setSlots(slots(), **change)
            self.assertIsNone(chart.hover, change)
        chart.setSlots(slots())
        chart.hover = 40
        chart.setSlots(slots()[:20])
        self.assertIsNone(chart.hover)

    def testASameRangeRefreshMovesTheBarsToTheirNewHeights(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=40))
        chart.show()
        before = chart.barRect(3)
        chart.setSlots(slots(percent=80))
        after = chart.barRect(3)
        self.assertEqual(chart.morphProgress, 0.0)
        self.assertEqual(chart.shownRect(3), before)
        chart.morph.setCurrentTime(50)
        halfway = chart.shownRect(3)
        self.assertLess(after.y(), halfway.y())
        self.assertLess(halfway.y(), before.y())
        chart.morph.setCurrentTime(100)
        self.assertEqual(chart.morphProgress, 1.0)
        self.assertEqual(chart.shownRect(3), after)

    def testANewRangeGrowsItsBarsFromTheBottom(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots(slots(percent=40))
        chart.show()
        chart.setSlots(slots(percent=80), rangeName="day", growMs=100)
        after = chart.barRect(3)
        self.assertEqual(chart.fromRects, [None] * len(chart.slots))
        self.assertEqual(chart.morph.duration(), 100)
        self.assertEqual(chart.shownRect(3), QRectF(after.x(), after.bottom(), after.width(), 0.0))
        chart.morph.setCurrentTime(50)
        halfway = chart.shownRect(3)
        self.assertLess(after.y(), halfway.y())
        self.assertLess(halfway.y(), after.bottom())
        chart.morph.setCurrentTime(100)
        self.assertEqual(chart.shownRect(3), after)

    def testAMessageOrAHiddenChartDoesntGrow(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots(slots(percent=40), rangeName="day", growMs=100)
        self.assertIsNone(chart.fromRects)
        self.assertEqual(chart.morphProgress, 1.0)
        chart.show()
        chart.setSlots([], rangeName="day", message="Loading", growMs=100)
        self.assertIsNone(chart.fromRects)
        self.assertEqual(chart.morphProgress, 1.0)

    def testANewRefreshMidMorphStartsFromWhatIsDrawn(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=40))
        chart.show()
        chart.setSlots(slots(percent=80))
        chart.morph.setCurrentTime(50)
        drawn = chart.shownRect(3)
        chart.setSlots(slots(percent=20))
        self.assertEqual(chart.fromRects[3], drawn)
        self.assertEqual(chart.morphProgress, 0.0)

    def testABarThatHadNoRectGrowsFromItsBottom(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        first = slots(percent=40)
        first[3] = [first[3][0], None, None, None, False]
        chart.setSlots(first)
        chart.show()
        chart.setSlots(slots(percent=40))
        target = chart.barRect(3)
        start = chart.shownRect(3)
        self.assertEqual(start.y(), target.bottom())
        self.assertEqual(start.height(), 0.0)
        self.assertEqual(start.width(), target.width())

    def testOnlyASameRangeRefreshMorphs(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=40))
        chart.show()
        changes = (
            {"slots": slots(percent=80), "rangeName": "day"},
            {"slots": slots(percent=80), "message": "Loading"},
            {"slots": slots(percent=80)[:50]},
            {"slots": slots(percent=40)},
        )
        for change in changes:
            chart.setSlots(change["slots"], rangeName=change.get("rangeName", "week"), message=change.get("message"))
            self.assertIsNone(chart.fromRects)
            self.assertEqual(chart.morphProgress, 1.0)
            chart.setSlots(slots(percent=40))
            self.assertIsNone(chart.fromRects)

    def testSlotsThatMovedByASlotChangeAtOnce(self):
        # bars pair up by index, so after a shift of a slot each bar would move towards its neighbor's old height
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=40))
        chart.show()
        shifted = [[slot[0] + 7200, 80] + slot[2:] for slot in slots()]
        chart.setSlots(shifted)
        self.assertIsNone(chart.fromRects)
        self.assertEqual(chart.morphProgress, 1.0)
        nudged = [[slot[0] + 7200 + 300, 40] + slot[2:] for slot in slots()]
        chart.setSlots(nudged)
        self.assertIsNotNone(chart.fromRects)
        self.assertEqual(chart.morph.state(), QAbstractAnimation.State.Running)

    def testARefreshThatChangesNothingLeavesARunningMorphAlone(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=40))
        chart.show()
        chart.setSlots(slots(percent=80))
        chart.morph.setCurrentTime(50)
        started = list(chart.fromRects)
        chart.setSlots(slots(percent=80))
        self.assertEqual(chart.morph.state(), QAbstractAnimation.State.Running)
        self.assertEqual(chart.fromRects, started)
        self.assertEqual(chart.morph.currentTime(), 50)

    def testTheBarsAreDrawnFromTheirMorphingRects(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.morphMs = 100
        chart.setSlots(slots(percent=90))
        chart.show()
        old = chart.barRect(3)
        chart.setSlots(slots(percent=10))
        # progress stays at 0, so the old tall bar is what's drawn
        self.assertEqual(chart.morphProgress, 0.0)
        image = rendered(chart)
        self.assertGreater(image.pixelColor(int(old.center().x()), int(old.y() + 5)).alpha(), 0)
        chart.morph.setCurrentTime(100)
        image = rendered(chart)
        self.assertEqual(image.pixelColor(int(old.center().x()), int(old.y() + 5)).alpha(), 0)

    def testAZeroDurationOrAHiddenChartChangesAtOnce(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots(slots(percent=40))
        chart.show()
        chart.setSlots(slots(percent=80))
        self.assertIsNone(chart.fromRects)
        chart.morphMs = 100
        chart.hide()
        chart.setSlots(slots(percent=40))
        self.assertIsNone(chart.fromRects)

    def testAVoltageAxisSpansTheWeek(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[NOW, None, None, 3800 + index, False] for index in range(84)], voltage=True)
        self.assertEqual(chart.scale()[:2], (3800, 3883))


    def spans(self, count, seconds, low=20, high=90, charges=0):
        return [[NOW - (count - 1 - index) * seconds, low, high, charges] for index in range(count)]

    def testEachRangeTitlesItsChartAndSaysWhenItsEmpty(self):
        chart = self.make(charts.LevelChart())
        for rangeName, title, empty in (("day", "Battery Level, Last 24 Hours", "No readings in the last 24 hours"), ("halfYear", "Battery Level, Last 6 Months", "No readings in the last 6 months"), ("all", "Battery Level, All Time", "No readings yet")):
            chart.setSlots([], rangeName=rangeName)
            self.assertEqual((chart.title, chart.emptyText()), (title, empty), rangeName)
            rendered(chart)

    def testASpanBarRunsFromItsLowestToItsHighest(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(60, 12 * 60 * 60), rangeName="month")
        rendered(chart)
        area = chart.area()
        bar = chart.barRect(10)
        self.assertAlmostEqual(bar.top(), area.bottom() - area.height() * 0.9, delta=0.5)
        self.assertAlmostEqual(bar.bottom(), area.bottom() - area.height() * 0.2, delta=0.5)

    def testAFlatSpanStillShows(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(60, 12 * 60 * 60, 50, 50), rangeName="month")
        rendered(chart)
        self.assertGreaterEqual(chart.barRect(0).height(), 3)

    def testAFewMonthsKeepTheirBarsNarrow(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(2, 30 * 24 * 60 * 60), rangeName="all")
        rendered(chart)
        self.assertLessEqual(chart.barRect(0).width(), charts.MAX_BAR_WIDTH)

    def testTheDaysLabelsFallOnSixHourMarks(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[NOW - (47 - index) * 1800, 60, None, None, False] for index in range(48)], rangeName="day")
        rendered(chart)
        texts = [text for index, text in chart.axisLabels()]
        self.assertTrue(texts)
        self.assertTrue(all(text in ("00:00", "06:00", "12:00", "18:00") for text in texts), texts)

    def testLongRangesLabelMonths(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(52, 7 * 24 * 60 * 60), rangeName="year")
        rendered(chart)
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        labels = chart.axisLabels()
        self.assertTrue(all(text in names or text.isdigit() for index, text in labels), labels)
        self.assertGreaterEqual(len(labels), 6)

    def monthSlots(self, count):
        ends = []
        year, month = 2025, 9
        for index in range(count):
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            ends.append(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)))
        return [[end, 20, 90, 1] for end in ends]

    def testAllNamesEveryMonthUnderItsOwnBar(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.monthSlots(14), rangeName="all")
        rendered(chart)
        labels = chart.axisLabels()
        self.assertEqual([index for index, text in labels], list(range(14)))
        # the month after December is named by its year, so two Octobers can be told apart
        self.assertEqual([text for index, text in labels][:5], ["Sep", "Oct", "Nov", "Dec", "2026"])
        self.assertAlmostEqual(chart.labelCenter(3), chart.barRect(3).center().x(), delta=0.5)

    def testTheLongerRangesNameJanuaryByItsYear(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[time.mktime((2026, 3, 1, 0, 0, 0, 0, 0, -1)) - (51 - index) * 7 * 24 * 60 * 60, 20, 90, 0] for index in range(52)], rangeName="year")
        rendered(chart)
        self.assertIn("2026", [text for index, text in chart.axisLabels()])

    def testHoveringASpanSaysItsLevelsAndCharges(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(26, 7 * 24 * 60 * 60, charges=2), rangeName="halfYear")
        text = chart.hoverText(3)
        self.assertIn("20% to 90%", text)
        self.assertIn("charged 2 times", text)
        chart.setSlots(self.spans(26, 7 * 24 * 60 * 60, 3700, 3900, 1), voltage=True, rangeName="halfYear")
        self.assertIn("3700 mV to 3900 mV, charged", chart.hoverText(0))

    def testARoughLevelSpanIsNamed(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(self.spans(26, 7 * 24 * 60 * 60, charts.LEVEL_HEIGHTS["low"], charts.LEVEL_HEIGHTS["good"]), rangeName="halfYear", rough=True)
        self.assertIn("low to good", chart.hoverText(0))

    def barState(self, chart, index):
        return chart.barColor(index).name()

    def testBarsAreColoredByState(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots([[NOW - 7200, 100, None, None, True], [NOW - 3600, 15, None, None, False], [NOW, 60, None, None, False]])
        theme = chart.theme
        self.assertEqual(self.barState(chart, 0), QColor(theme.green).name())
        self.assertEqual(self.barState(chart, 1), QColor(theme.red).name())
        self.assertEqual(self.barState(chart, 2), QColor(theme.blue).name())
        # the tray icon's rule is 20% or below
        chart.setSlots([[NOW - 3600, 20, None, None, False], [NOW, 21, None, None, False]])
        self.assertEqual((self.barState(chart, 0), self.barState(chart, 1)), (QColor(theme.red).name(), QColor(theme.blue).name()))

    def testASpanBarUsesItsHighLevelAndItsChargeCount(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots([[NOW - 86400, 20, 90, 1], [NOW - 43200, 5, 15, 0], [NOW, 20, 90, 0]], rangeName="month")
        theme = chart.theme
        self.assertEqual([self.barState(chart, index) for index in range(3)], [QColor(theme.green).name(), QColor(theme.red).name(), QColor(theme.blue).name()])

    def testAVoltageChartKeepsOneColor(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots([[NOW - 7200, None, None, 3300, True], [NOW, None, None, 3900, False]], voltage=True)
        self.assertEqual({self.barState(chart, 0), self.barState(chart, 1)}, {QColor(chart.theme.blue).name()})

    def testALowBarIsRedInTheRender(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[NOW - 7200 * (83 - index), 15, None, None, False] for index in range(84)])
        image = rendered(chart)
        bar = chart.barRect(40)
        color = image.pixelColor(int(bar.center().x()), int(bar.bottom() - 2))
        self.assertGreater(color.red(), color.blue() + 60)

    def testEveryBarIsSolidAtItsMiddle(self):
        chart = self.make(charts.LevelChart())
        levels = [100, 100, 95, 60, 50, 20, 3, 1]
        chart.setSlots([[NOW - 7200 * (83 - index), levels[index % len(levels)], None, None, index % 9 == 0] for index in range(84)])
        image = rendered(chart)
        for index in range(84):
            bar = chart.barRect(index)
            self.assertGreater(image.pixelColor(int(bar.center().x()), int(bar.center().y())).alpha(), 120, index)

    def testARoughLowBarIsRedLikeTheIconAndGoodIsNot(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        heights = charts.LEVEL_HEIGHTS
        chart.setSlots([[NOW - 7200 * (3 - index), None, name, None, False] for index, name in enumerate(("low", "critical", "normal", "good"))], rough=True)
        self.assertEqual([chart.barColor(index).name() for index in range(4)], [QColor(chart.theme.red).name()] * 2 + [QColor(chart.theme.blue).name()] * 2)
        chart.setSlots([[NOW - 86400, 5, heights["low"], 0], [NOW, heights["good"], heights["good"], 0]], rangeName="month", rough=True)
        self.assertEqual([chart.barColor(index).name() for index in range(2)], [QColor(chart.theme.red).name(), QColor(chart.theme.blue).name()])

    def testTheDayAndWeekReadoutsSayCharging(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[NOW - 7200, 85, None, None, True], [NOW, 85, None, None, False]])
        self.assertTrue(chart.hoverText(0).endswith("85%, charging"))
        self.assertTrue(chart.hoverText(1).endswith("85%"))
        chart.setSlots([[NOW - 7200, None, None, 3900, True]], voltage=True)
        self.assertTrue(chart.hoverText(0).endswith("3900 mV, charging"))
        chart.setSlots([[NOW - 7200, None, "good", None, True]], rough=True)
        self.assertTrue(chart.hoverText(0).endswith("good, charging"))

    def testABarIsAtMostTwentyFourPixelsWide(self):
        self.assertEqual(charts.MAX_BAR_WIDTH, 24)
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots([[NOW - 7200, 50, None, None, False], [NOW, 50, None, None, False]])
        self.assertEqual(chart.barRect(0).width(), 24)

    def testEachRunOfChargingSlotsGetsOneDotAboveItsFirstBar(self):
        chart = self.make(charts.LevelChart())
        chart.resize(700, 238)
        chart.setSlots([[NOW - 7200 * (83 - index), 50, None, None, index in (3, 4, 5, 10)] for index in range(84)])
        dots = []
        with unittest.mock.patch.object(charts.Chart, "paintDot", lambda self, painter, center, color: dots.append((center.x(), center.y(), color.name()))):
            rendered(chart)
        self.assertEqual(len(dots), 2)
        for dot, index in zip(dots, (3, 10)):
            bar = chart.barRect(index)
            self.assertAlmostEqual(dot[0], bar.center().x(), delta=0.01)
            self.assertAlmostEqual(dot[1], bar.top() - 8, delta=0.01)
            self.assertEqual(dot[2], QColor(chart.theme.green).name())

    def testNoChargeMeansNoDot(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots(slots())
        dots = []
        with unittest.mock.patch.object(charts.Chart, "paintDot", lambda self, painter, center, color: dots.append(center)):
            rendered(chart)
        self.assertEqual(dots, [])

    def testTheMonthlyChartsSayTheyCoverAllTime(self):
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, "h"))
        self.assertEqual(chart.title, "Hours per Charge, All Time")

class ChartDescriptionTests(ChartCase):
    # the charts are painted, so the description is all a screen reader gets of what each one shows
    def testALevelChartDescribesItsMessageOrItsNewestBar(self):
        chart = self.make(charts.LevelChart())
        self.assertEqual(chart.accessibleDescription(), charts.RANGE_EMPTY["week"])
        chart.setSlots([[NOW, None, None, None, False]] * 84)
        self.assertEqual(chart.accessibleDescription(), charts.LEVEL_EMPTY)
        chart.setSlots(slots(percent=62)[:-1] + [[NOW, None, None, None, False]])
        self.assertEqual(chart.accessibleDescription(), "Battery Level, Last 7 Days, latest " + chart.hoverText(82))
        self.assertTrue(chart.accessibleDescription().endswith(", 62%"), chart.accessibleDescription())
        chart.setSlots([], rangeName="month", message=historyWindow.LOADING_TEXT)
        self.assertEqual(chart.accessibleDescription(), historyWindow.LOADING_TEXT)
        chart.setSlots([], rangeName="month", message=historyWindow.RANGE_ERROR_TEXT)
        self.assertEqual(chart.accessibleDescription(), historyWindow.RANGE_ERROR_TEXT)
        chart.setSlots([[NOW - 7 * 24 * 60 * 60, 30, 90, 1], [NOW, 40, 70, 0]], rangeName="month")
        self.assertEqual(chart.accessibleDescription(), "Battery Level, Last 30 Days, latest " + chart.hoverText(1))
        self.assertTrue(chart.accessibleDescription().endswith(", 40% to 70%"), chart.accessibleDescription())
        self.assertEqual(chart.accessibleName(), "Battery Level, Last 30 Days")

    def testALineChartDescribesItsHeadlineOrItsEmptyText(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min"))
        self.assertEqual(chart.accessibleDescription(), charts.CHARGING_EMPTY)
        thisMonth = time.strftime("%Y-%m", time.gmtime())
        chart.setSeries({"charger": [("2026-01", 6.0), (thisMonth, 4.0)]})
        self.assertEqual(chart.accessibleDescription(), "4 min per 10%, this month")
        chart.setSeries({"charger": []})
        self.assertEqual(chart.accessibleDescription(), charts.CHARGING_EMPTY)


class EnglishNameTests(ChartCase):
    def namedStrftime(self, asked):
        real = time.strftime

        def strftime(fmt, *args):
            # strptime asks for names itself to build its tables, so only the window's own calls count
            if (sys._getframe(1).f_globals["__name__"] == "charts"):
                asked.append(fmt)
            # stands in for a locale whose names differ, whichever one this machine has installed
            return real(re.sub("%[aAbB]", "?", fmt), *args)
        return unittest.mock.patch.object(time, "strftime", strftime)

    def at(self, month, day, hour=0):
        return time.mktime((2026, month, day, hour, 0, 0, 0, 0, -1))

    def testTheWindowNeverAsksTheLocaleForAName(self):
        asked = []
        week = self.make(charts.LevelChart())
        week.setSlots([[self.at(10, 5, 2) + index * 7200, 62, None, None, False] for index in range(84)])
        month = self.make(charts.LevelChart())
        month.setSlots([[self.at(10, 9, 12), 20, 90, 1]] * 60, rangeName="month")
        line = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY))
        line.setSeries({"charger": [("2026-08", 4.0), ("2026-09", 5.0), ("2026-10", 4.5)]})
        with self.namedStrftime(asked):
            for chart in (week, month, line):
                rendered(chart)
            week.axisLabels()
            for rangeName in history.LEVEL_RANGES:
                chart = self.make(charts.LevelChart())
                chart.setSlots([[self.at(10, 9, 12), 20, 90, 1]] if rangeName in charts.SPAN_RANGES else [[self.at(10, 9, 12), 20, None, None, False]], rangeName=rangeName)
                chart.hoverText(0)
            charts.latestText(41.6, "h", "2026-03", self.at(10, 9, 12))
        self.assertTrue(asked)
        self.assertEqual([fmt for fmt in asked if re.search("%[aAbB]", fmt)], [])

    def testTheAxisAndHoverNamesAreEnglish(self):
        chart = self.make(charts.LevelChart())
        chart.setSlots([[self.at(10, 5, 2) + index * 7200, 62, None, None, False] for index in range(84)])
        chart.resize(700, 238)
        self.assertEqual(chart.axisLabels(), [(12, "Tue"), (24, "Wed"), (36, "Thu"), (48, "Fri"), (60, "Sat"), (72, "Sun")])
        self.assertEqual(chart.hoverText(3), "Mon 08:00, 62%")
        cases = (("month", self.at(10, 9, 12), "Oct 9, 00:00 to 12:00, 20% to 90%, charged"), ("halfYear", self.at(10, 9), "Week of Oct 2, 20% to 90%, charged"), ("all", self.at(11, 1), "October 2026, 20% to 90%, charged"))
        for rangeName, end, expected in cases:
            chart.setSlots([[end, 20, 90, 1]], rangeName=rangeName)
            self.assertEqual(chart.hoverText(0), expected, rangeName)

    def testTheMonthNamesAreEnglishUnderAnotherLocale(self):
        saved = locale.setlocale(locale.LC_TIME)
        chosen = None
        for name in ("de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "ja_JP.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            chosen = name
            break
        if (chosen is None):
            self.skipTest("no non English locale is installed")
        try:
            self.assertEqual(charts.latestText(41.6, "h", "2026-03", self.at(10, 9, 12)), "42 h, Mar 2026")
            chart = self.make(charts.LevelChart())
            chart.setSlots([[self.at(3, 5), 20, 90, 1]], rangeName="all")
            self.assertEqual(chart.hoverText(0), "March 2026, 20% to 90%, charged")
            chart.setSlots([[self.at(10, 5, 2), 62, None, None, False]])
            self.assertEqual(chart.hoverText(0), "Mon 02:00, 62%")
        finally:
            locale.setlocale(locale.LC_TIME, saved)


class RangeTableTests(unittest.TestCase):
    def testTheChartsTablesFollowTheServicesRanges(self):
        names = set(history.LEVEL_RANGES)
        self.assertEqual(charts.SPAN_RANGES, history.SPAN_RANGES)
        for table in (charts.RANGE_TITLES, charts.RANGE_EMPTY, charts.LABEL_SPACING, charts.RANGE_SECONDS):
            self.assertEqual(set(table), names)
        for name, (count, seconds) in history.LEVEL_RANGES.items():
            self.assertEqual(charts.RANGE_SECONDS[name], seconds, name)

    def testTheWindowsRangeControlFollowsTheServicesRanges(self):
        self.assertEqual(historyWindow.RANGE_NAMES, ("day", "week", "month"))
        self.assertTrue(set(historyWindow.RANGE_NAMES) <= set(history.LEVEL_RANGES))
        self.assertEqual(len(historyWindow.RANGE_LABELS), len(historyWindow.RANGE_NAMES))
        self.assertEqual(len(historyWindow.RANGE_WIDTHS), len(historyWindow.RANGE_NAMES))


class NiceTickTests(unittest.TestCase):
    def testTicksStartAtZeroAndCoverTheTopWithRoundSteps(self):
        self.assertEqual(charts.niceTicks(42), [0, 20, 40, 60])
        self.assertEqual(charts.niceTicks(11), [0, 5, 10, 15])
        self.assertEqual(charts.niceTicks(4.5), [0, 2, 4, 6])
        self.assertEqual(charts.niceTicks(8), [0, 2.5, 5, 7.5, 10])
        self.assertEqual(charts.niceTicks(100), [0, 50, 100, 150])

    def testEveryTopGetsThreeToFiveTicksThatCoverIt(self):
        for top in (0.05, 0.7, 1, 3.3, 9, 10, 12, 42, 55, 99, 101, 640, 7000):
            ticks = charts.niceTicks(top)
            self.assertTrue(3 <= len(ticks) <= 5, (top, ticks))
            self.assertEqual(ticks[0], 0)
            self.assertGreaterEqual(ticks[-1], top * 1.1 - 1e-9, (top, ticks))

    def testAZeroTopStillGetsTicks(self):
        self.assertGreaterEqual(len(charts.niceTicks(0)), 3)


class LineChartTests(ChartCase):
    def hours(self, values=((("2026-08", 31.0), ("2026-09", 36.0), ("2026-10", 41.0)))):
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, "h"))
        chart.resize(700, 238)
        chart.setSeries({"hours": list(values)})
        return chart

    def testTheYAxisStartsAtZeroWithRoundLabelsAndAShortUnit(self):
        chart = self.hours()
        self.assertEqual(chart.tickLabels(), ["60 h", "40 h", "20 h", "0 h"])
        small = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min"))
        small.setSeries({"charger": [("2026-09", 9.0), ("2026-10", 10.0)]})
        self.assertEqual(small.tickLabels(), ["15 min", "10 min", "5 min", "0 min"])
        fraction = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min"))
        fraction.setSeries({"charger": [("2026-09", 7.0), ("2026-10", 8.0)]})
        self.assertEqual(fraction.tickLabels(), ["10 min", "7.5 min", "5 min", "2.5 min", "0 min"])

    def testAWobbleStaysSmallOnAZeroBasedAxis(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min"))
        chart.resize(700, 238)
        chart.setSeries({"charger": [("2026-09", 9.0), ("2026-10", 10.0)]})
        drawn = []
        with unittest.mock.patch.object(charts.LineChart, "paintLine", lambda self, painter, area, positions, color, fade: drawn.append(positions)):
            rendered(chart)
        first, second = drawn[0]
        self.assertLess(first.y() - second.y(), chart.area().height() * 0.1)

    def testTheAxisWidthFitsTheWidestLabel(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min"))
        chart.setSeries({"charger": [("2026-09", 13.0), ("2026-10", 15.0)]})
        metrics = QFontMetricsF(charts.font(charts.LABEL_SIZE))
        widest = max(metrics.horizontalAdvance(label) for label in chart.tickLabels())
        self.assertGreaterEqual(chart.axisWidth(), widest + 8)
        self.assertGreaterEqual(chart.axisWidth(), charts.AXIS_WIDTH)
        chart.setSeries({"charger": [("2026-09", 13000.0), ("2026-10", 15000.0)]})
        wide = QFontMetricsF(charts.font(charts.LABEL_SIZE)).horizontalAdvance("20000 min")
        self.assertGreaterEqual(chart.axisWidth(), wide + 8)
        self.assertGreater(chart.axisWidth(), charts.AXIS_WIDTH)
        chart.resize(700, 238)
        self.assertAlmostEqual(chart.area().right(), 700 - 16 - chart.axisWidth(), delta=0.01)

    def testTheLevelChartKeepsItsAxisWidth(self):
        chart = self.make(charts.LevelChart())
        self.assertEqual(chart.axisWidth(), charts.AXIS_WIDTH)

    def testTheHoursLineIsBlueAndNeverOrange(self):
        chart = self.hours()
        drawn = []
        with unittest.mock.patch.object(charts.LineChart, "paintLine", lambda self, painter, area, positions, color, fade: drawn.append(color.name())):
            rendered(chart)
        self.assertEqual(drawn, [QColor(chart.theme.blue).name()])

    def testChargersGetTheTwoChargeColorsThenTheSecondaryText(self):
        for theme, expected in ((glass.DARK, ("#24A957", "#5E5CE6")), (glass.LIGHT, ("#248A3D", "#5856D6"))):
            self.assertEqual(theme.chargeSeries, expected)
            chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min", byCharger=True))
            chart.setTheme(theme)
            chart.setSeries({name: [("2026-09", 4.0), ("2026-10", 5.0)] for name in ("cable", "wireless charger", "charger 3")})
            drawn = []
            with unittest.mock.patch.object(charts.LineChart, "paintLine", lambda self, painter, area, positions, color, fade: drawn.append(color.name())):
                rendered(chart)
            self.assertEqual(drawn, [QColor(expected[0]).name(), QColor(expected[1]).name(), QColor(theme.secondary).name()])
            self.assertNotIn(QColor(theme.orange).name(), drawn)

    def testThePlotSitsBelowTheHeadline(self):
        chart = self.hours()
        self.assertGreaterEqual(chart.area().top(), charts.TOP - 2 + 26 + 4)

    def testLongHistoriesKeepOnlyTheNewestDot(self):
        months = ["%04d-%02d" % (2017 + index // 12, index % 12 + 1) for index in range(120)]
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, "h"))
        chart.resize(700, 238)
        chart.setSeries({"hours": [(month, 40.0) for month in months]})
        dots = []
        with unittest.mock.patch.object(charts.Chart, "paintDot", lambda self, painter, center, color: dots.append(center.x())):
            rendered(chart)
        self.assertEqual(len(dots), 1)
        self.assertAlmostEqual(dots[0], chart.area().right(), delta=0.01)
        # twenty-five months across the same width are about 25 px apart, so every one keeps its dot
        chart.setSeries({"hours": [(month, 40.0) for month in months[:25]]})
        dots.clear()
        with unittest.mock.patch.object(charts.Chart, "paintDot", lambda self, painter, center, color: dots.append(center.x())):
            rendered(chart)
        self.assertEqual(len(dots), 25)

    def testEveryMonthsPointGetsADot(self):
        chart = self.hours()
        dots = []
        with unittest.mock.patch.object(charts.Chart, "paintDot", lambda self, painter, center, color: dots.append(color.name())):
            rendered(chart)
        self.assertEqual(dots, [QColor(chart.theme.blue).name()] * 3)

    def testTheCaptionIsGone(self):
        self.assertFalse(hasattr(charts, "HOURS_CAPTION"))
        chart = self.hours()
        self.assertFalse(hasattr(chart, "caption"))


    def testTheBigNumberIsTheNewestMonthAcrossEverySeries(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY))
        chart.setSeries({"charger": [("2026-08", 6.0), ("2026-09", 5.0)], "wireless charger": [("2026-09", 4.0), ("2026-10", 3.0)]})
        self.assertEqual(chart.headline(), ("2026-10", 3.0))
        shown = []
        with unittest.mock.patch.object(charts, "latestText", lambda value, unit, month, now: shown.append((value, month)) or ""):
            rendered(chart)
        self.assertEqual(shown, [(3.0, "2026-10")])

    def testEverySeriesGetsALine(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min", byCharger=True))
        chart.setSeries({name: [("2026-09", 4.0 + index), ("2026-10", 5.0)] for index, name in enumerate(("a", "b", "c", "d", "e"))})
        drawn = []
        with unittest.mock.patch.object(charts.LineChart, "paintLine", lambda self, painter, area, positions, color, fade: drawn.append(color.name())):
            rendered(chart)
        self.assertEqual(len(drawn), 5)
        self.assertEqual(drawn[3], drawn[0])

    def testAnEmptySeriesSaysSo(self):
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, "h"))
        chart.setSeries({"hours": []})
        self.assertEqual(chart.series, {})
        image = rendered(chart)
        # the message is drawn across the middle, as wide as its text, and the title's band at the top is left out of the count
        middle = image.height() // 2
        inked = [x for x in range(image.width()) for y in range(middle - 10, middle + 10) if image.pixelColor(x, y).alpha() > 100]
        width = QFontMetrics(charts.font(13)).horizontalAdvance(charts.HOURS_EMPTY)
        self.assertTrue(inked)
        self.assertGreater(max(inked) - min(inked), width * 0.8)
        self.assertLess(abs((max(inked) + min(inked)) / 2 - image.width() / 2), 10)

    def testASeriesDraws(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY))
        chart.setSeries({"wireless charger": [("2026-09", 3.2), ("2026-10", 2.8)], "charger 1": [("2026-10", 6.0)]})
        self.assertEqual(chart.months(), ["2026-09", "2026-10"])
        image = rendered(chart)
        area = chart.area()
        top = chart.ticks()[-1]
        # halfway between the two months the curve passes through the mean of the two values, well clear of both dots
        x = round(area.x() + area.width() / 2)
        y = area.bottom() - (3.2 + 2.8) / 2 / top * area.height()
        grid = [area.y() + area.height() * index / (len(chart.ticks()) - 1) for index in range(len(chart.ticks()))]
        self.assertGreater(min(abs(y - line) for line in grid), 4)
        blue = QColor(chart.theme.blue)
        near = [image.pixelColor(x, round(y) + step) for step in range(-2, 3)]
        self.assertTrue(any(color.alpha() > 200 and abs(color.red() - blue.red()) + abs(color.green() - blue.green()) + abs(color.blue() - blue.blue()) < 40 for color in near), [color.name() for color in near])
        # and nothing is drawn well above or below the line there
        self.assertEqual([image.pixelColor(x, round(y) + step).alpha() for step in (-12, 12)], [0, 0])


    def testMonthLabelsThinOutSoTheyDontOverlap(self):
        # 40 months across 600 px put the labels about 15 px apart
        places = [index * 600 / 39 for index in range(40)]
        picked = charts.labeledMonths(places)
        self.assertIn(39, picked)
        self.assertTrue(all(abs(places[a] - places[b]) >= charts.MONTH_LABEL_WIDTH for a, b in zip(picked, picked[1:])))
        self.assertEqual(charts.labeledMonths([0, 300, 600]), [0, 1, 2])

    def testTheLatestFigureNamesAMonthThatIsntThisOne(self):
        now = time.mktime((2026, 10, 9, 12, 0, 0, 0, 0, -1))
        self.assertEqual(charts.latestText(41.6, "h", "2026-10", now), "42 h, this month")
        self.assertEqual(charts.latestText(41.6, "h", "2026-03", now), "42 h, Mar 2026")

    def testThisMonthIsTheMonthTheFiguresAreGroupedBy(self):
        # the service groups the figures by UTC month, so late on October 31 in California the newest figure is November's
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            now = calendar.timegm((2026, 11, 1, 3, 0, 0, 0, 0, 0))
            self.assertEqual(charts.latestText(12, "h", "2026-11", now), "12 h, this month")
        finally:
            if (saved is None):
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def testTheLatestFigureIsntCutShort(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY))
        chart.setSeries({"charger 1": [(time.strftime("%Y-%m", time.gmtime()), 4.0)]})
        shown = []
        real = charts.latestText
        with unittest.mock.patch.object(charts, "latestText", lambda *args: shown.append(real(*args)) or shown[-1]):
            image = rendered(chart)
        self.assertEqual(shown, ["4 min per 10%, this month"])
        inked = [x for x in range(image.width()) for y in range(30, 43) if image.pixelColor(x, y).alpha() > 100]
        # the text's width depends on the font each system falls back to, so the box it must fit is measured too
        width = QFontMetrics(charts.font(22, QFont.Weight.Bold)).horizontalAdvance(shown[0])
        self.assertGreater(max(inked), 16 + min(width, chart.width() - 32 - charts.AXIS_WIDTH) - 8)


if (__name__ == "__main__"):
    unittest.main()
