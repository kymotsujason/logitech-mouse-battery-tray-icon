import os
import time
import unittest
import unittest.mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QImage
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


if (__name__ == "__main__"):
    unittest.main()
