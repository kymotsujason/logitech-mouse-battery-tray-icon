import calendar
import os
import time
import unittest
import unittest.mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont, QFontMetrics, QImage
from PyQt6.QtWidgets import QApplication

import charts
import glass

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
        names = [time.strftime("%b", time.strptime(str(month), "%m")) for month in range(1, 13)]
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

    def testTheMonthlyChartsSayTheyCoverAllTime(self):
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, charts.HOURS_CAPTION))
        self.assertEqual(chart.title, "Hours per Charge, All Time")

class LineChartTests(ChartCase):
    def testAnEmptySeriesSaysSo(self):
        chart = self.make(charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, charts.HOURS_CAPTION))
        chart.setSeries({"hours": []})
        self.assertEqual(chart.series, {})
        rendered(chart)

    def testASeriesDraws(self):
        chart = self.make(charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY))
        chart.setSeries({"wireless charger": [("2026-09", 4.2), ("2026-10", 4.0)], "charger 1": [("2026-10", 6.0)]})
        self.assertEqual(chart.months(), ["2026-09", "2026-10"])
        rendered(chart)


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
        chart.setSeries({"charger 1": [(time.strftime("%Y-%m"), 4.0)]})
        image = rendered(chart)
        inked = [x for x in range(image.width()) for y in range(30, 43) if image.pixelColor(x, y).alpha() > 100]
        # the text's width depends on the font each system falls back to, so the box it must fit is measured too
        width = QFontMetrics(charts.font(22, QFont.Weight.Bold)).horizontalAdvance("4 min per 10%, this month")
        self.assertGreater(max(inked), 16 + min(width, chart.width() - 32 - charts.AXIS_WIDTH) - 8)

if (__name__ == "__main__"):
    unittest.main()
