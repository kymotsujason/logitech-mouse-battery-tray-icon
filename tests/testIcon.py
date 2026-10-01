import os
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtGui import QColor, QFontDatabase
from PyQt6.QtWidgets import QApplication

import icon
from icon import IconState, makeIcon, renderImage

app = QApplication.instance() or QApplication([])
WHITE = QColor("#FCFCFC")


def alphas(image, left, top, right, bottom):
    return [image.pixelColor(x, y).alpha() for y in range(top, bottom) for x in range(left, right)]


class IconTests(unittest.TestCase):
    def testSizes(self):
        for size in (22, 44):
            image = renderImage(IconState(percent=81), size, WHITE)
            self.assertEqual((image.width(), image.height()), (size, size))

    def testNumberIsCutOutOfTheFill(self):
        image = renderImage(IconState(percent=81), 22, WHITE)
        self.assertTrue(any(a < 40 for a in alphas(image, 7, 11, 15, 18)))

    def testTraySizeDigitsAreCrisp(self):
        # inside the fill, each digit pixel is either fully cut out or fully kept
        area = alphas(renderImage(IconState(percent=81), 22, WHITE), 7, 11, 15, 18)
        self.assertTrue(all(a <= 10 or a >= 245 for a in area))

    def testNumberOverTheTrackIsSolid(self):
        # at 18% the fill starts at row 17, so rows 11 to 16 of the number sit on the track
        area = alphas(renderImage(IconState(percent=18), 22, WHITE), 7, 11, 15, 17)
        self.assertTrue(any(a >= 200 for a in area))
        self.assertFalse(any(a < 40 for a in area))

    def testLowNumberIsRed(self):
        # at 5% the fill is one thin row, so the number itself has to carry the warning
        for percent in (5, 18, 20):
            image = renderImage(IconState(percent=percent), 22, WHITE)
            solid = [image.pixelColor(x, y) for y in range(11, 17) for x in range(7, 15) if image.pixelColor(x, y).alpha() >= 200]
            self.assertTrue(solid)
            for color in solid:
                self.assertGreater(color.red(), 200)
                self.assertLess(color.green(), 120)

    def testNumberKeepsTheBaseColorOtherwise(self):
        for state in (IconState(percent=21), IconState(percent=18, charging=True)):
            image = renderImage(state, 22, WHITE)
            solid = [image.pixelColor(x, y) for y in range(11, 16) for x in range(7, 15) if image.pixelColor(x, y).alpha() >= 200]
            self.assertTrue(solid)
            for color in solid:
                self.assertGreater(color.green(), 240)

    def testFillColors(self):
        normal = renderImage(IconState(percent=81), 22, WHITE).pixelColor(11, 19)
        low = renderImage(IconState(percent=18), 22, WHITE).pixelColor(11, 19)
        charging = renderImage(IconState(percent=81, charging=True), 22, WHITE).pixelColor(11, 19)
        self.assertEqual(normal.alpha(), 255)
        self.assertGreater(normal.red(), 240)
        self.assertGreater(low.red(), 200)
        self.assertLess(low.green(), 120)
        self.assertGreater(charging.green(), 150)
        self.assertLess(charging.red(), 100)

    def testAsleepIsGray(self):
        color = renderImage(IconState(percent=81, asleep=True), 22, WHITE).pixelColor(11, 19)
        self.assertAlmostEqual(color.red(), 0x8E, delta=2)
        self.assertAlmostEqual(color.green(), 0x8E, delta=2)
        self.assertAlmostEqual(color.blue(), 0x93, delta=2)

    def testEmptyHasNoFill(self):
        image = renderImage(IconState(empty=True), 22, WHITE)
        self.assertLess(image.pixelColor(11, 19).alpha(), 100)

    def testNoPercentFillsWithoutANumber(self):
        image = renderImage(IconState(percent=None), 22, WHITE)
        self.assertFalse(any(a < 40 for a in alphas(image, 7, 11, 15, 18)))
        self.assertEqual(image.pixelColor(11, 12).alpha(), 255)

    def testHundredStaysInsideTheBody(self):
        image = renderImage(IconState(percent=100, charging=True), 22, WHITE)
        self.assertTrue(any(a < 40 for a in alphas(image, 5, 11, 17, 16)))
        self.assertGreater(image.pixelColor(4, 14).alpha(), 200)
        self.assertGreater(image.pixelColor(17, 14).alpha(), 200)

    def testIconHoldsEverySize(self):
        sizes = makeIcon(IconState(percent=81), WHITE).availableSizes()
        self.assertEqual(sorted(size.width() for size in sizes), [16, 22, 24, 32, 44, 48, 64, 96, 128])

    def testSmallestSizeKeepsTheFillAndDropsTheNumber(self):
        image = renderImage(IconState(percent=81), 16, WHITE)
        self.assertFalse(any(a < 200 for a in alphas(image, 5, 8, 11, 13)))

    def testLargerSizesCutTheNumberOut(self):
        for size in icon.ICON_SIZES:
            if (size < icon.NUMBER_MIN_SIZE):
                continue
            k = size / 22
            image = renderImage(IconState(percent=81), size, WHITE)
            area = alphas(image, round(7 * k), round(11 * k), round(15 * k), round(18 * k))
            self.assertTrue(any(a < 40 for a in area), size)

    def testDigitsUseTheBundledFont(self):
        self.assertTrue(os.path.exists(icon.FONT_FILE))
        fontId = QFontDatabase.addApplicationFont(icon.FONT_FILE)
        self.assertGreaterEqual(fontId, 0)
        # the bundled family and the fallback share a name, so the fallback gets one no font has
        with mock.patch.object(icon, "FALLBACK_FAMILY", "No Such Family 0x7f3a"), mock.patch.object(icon, "digitFamilyName", None):
            self.assertEqual(icon.digitFamily(), QFontDatabase.applicationFontFamilies(fontId)[0])


if (__name__ == "__main__"):
    unittest.main()
