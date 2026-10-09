import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtGui import QColor
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

    def testPlateIsClearedAroundTheNumber(self):
        # the plate's border is one cell outside the digits, so it's always clear, whatever the level
        for percent in (5, 29, 81, 100):
            image = renderImage(IconState(percent=percent), 22, WHITE)
            cells, plate = icon.numberLayout(str(percent), 22)
            border = [(x, y) for x in range(plate.left(), plate.right() + 1) for y in (plate.top(), plate.bottom())]
            border += [(x, y) for y in range(plate.top(), plate.bottom() + 1) for x in (plate.left(), plate.right())]
            self.assertTrue(all(image.pixelColor(x, y).alpha() == 0 for x, y in border), percent)

    def testTraySizeDigitsAreCrisp(self):
        # inside the fill, each digit pixel is either fully cut out or fully kept
        area = alphas(renderImage(IconState(percent=81), 22, WHITE), 7, 11, 15, 18)
        self.assertTrue(all(a <= 10 or a >= 245 for a in area))

    def testFillNeverCrossesTheNumber(self):
        # a green charging fill shows up as any color in the plate that isn't the white of the digits
        for size in icon.ICON_SIZES:
            if (size < icon.NUMBER_MIN_SIZE):
                continue
            for percent in range(101):
                image = renderImage(IconState(percent=percent, charging=True), size, WHITE)
                cells, plate = icon.numberLayout(str(percent), size)
                for y in range(plate.top(), plate.bottom() + 1):
                    for x in range(plate.left(), plate.right() + 1):
                        color = image.pixelColor(x, y)
                        inCell = any(cell.contains(x, y) for cell in cells)
                        if (inCell):
                            self.assertEqual(color.rgba(), WHITE.rgba(), (size, percent, x, y))
                        else:
                            self.assertEqual(color.alpha(), 0, (size, percent, x, y))

    def testEveryPercentDrawsADifferentNumber(self):
        # 28 and 29 once drew alike where the fill line ran through them, so each number is compared inside its own plate
        for size in icon.ICON_SIZES:
            if (size < icon.NUMBER_MIN_SIZE):
                continue
            seen = {}
            for percent in range(101):
                image = renderImage(IconState(percent=percent), size, WHITE)
                cells, plate = icon.numberLayout(str(percent), size)
                pattern = tuple(image.pixelColor(x, y).alpha() for y in range(plate.top(), plate.bottom() + 1) for x in range(plate.left(), plate.right() + 1))
                self.assertNotIn((plate.width(), pattern), seen, (size, percent, seen.get((plate.width(), pattern))))
                seen[(plate.width(), pattern)] = percent

    def testNumberIsCenteredOnTheBody(self):
        for size in icon.ICON_SIZES:
            if (size < icon.NUMBER_MIN_SIZE):
                continue
            for text in ("8", "29", "100"):
                cells, plate = icon.numberLayout(text, size)
                left = min(cell.left() for cell in cells)
                right = max(cell.right() for cell in cells)
                self.assertLessEqual(abs((left + right + 1) / 2 - 11 * size / 22), 1, (size, text))
                self.assertGreaterEqual(plate.left(), round(4 * size / 22), (size, text))
                self.assertLessEqual(plate.right(), round(18 * size / 22) - 1, (size, text))

    def testWheelIsCutOutOfTheFillAndSolidOnTheTrack(self):
        full = renderImage(IconState(percent=100), 22, WHITE)
        half = renderImage(IconState(percent=50), 22, WHITE)
        # the wheel covers three quarters of columns 10 and 11 at this size, so neither is cut out or filled all the way
        for x in (10, 11):
            self.assertLess(full.pixelColor(x, 5).alpha(), 100)
            self.assertGreater(half.pixelColor(x, 5).alpha(), 150)

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

    def testPercentOutsideTheRangeDrawsTheNearestNumber(self):
        self.assertEqual(renderImage(IconState(percent=120), 22, WHITE), renderImage(IconState(percent=100), 22, WHITE))

    def testIconHoldsEverySize(self):
        sizes = makeIcon(IconState(percent=81), WHITE).availableSizes()
        self.assertEqual(sorted(size.width() for size in sizes), [16, 22, 24, 32, 44, 48, 64])

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

    def testNoFontFileShips(self):
        folder = os.path.dirname(icon.__file__)
        self.assertFalse(os.path.exists(os.path.join(folder, "NotoSans-Medium.ttf")))
        self.assertFalse(os.path.exists(os.path.join(folder, "OFL.txt")))


class WithoutNumberTests(unittest.TestCase):
    def testTheWindowsIconHasNoPlate(self):
        cells, plate = icon.numberLayout("62", 120)
        image = renderImage(IconState(percent=62), 120, WHITE, number=False)
        # the plate sits inside the fill at 62%, so with no number every pixel there is fill
        self.assertTrue(all(image.pixelColor(x, y).alpha() == 255 for y in range(plate.top(), plate.bottom() + 1) for x in range(plate.left(), plate.right() + 1)))

    def testTheWindowsIconKeepsItsColors(self):
        low = renderImage(IconState(percent=15), 120, WHITE, number=False).pixelColor(60, 110)
        charging = renderImage(IconState(percent=50, charging=True), 120, WHITE, number=False).pixelColor(60, 110)
        self.assertGreater(low.red(), 200)
        self.assertGreater(charging.green(), 150)

    def testTheTraysIconIsUnchanged(self):
        self.assertEqual(renderImage(IconState(percent=62), 22, WHITE), renderImage(IconState(percent=62), 22, WHITE, number=True))


if (__name__ == "__main__"):
    unittest.main()
