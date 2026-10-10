import os
import shutil
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QPalette
from PyQt6.QtWidgets import QApplication

import glass
import hidpp

app = QApplication.instance() or QApplication([])



def luminance(name):
    channels = [int(name[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(first, second):
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)

class ThemeTests(unittest.TestCase):
    def testThePortalsChoiceWins(self):
        light = QPalette()
        light.setColor(QPalette.ColorRole.Window, QColor("#FFFFFF"))
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
        self.assertTrue(glass.isDark(1, light))
        self.assertFalse(glass.isDark(2, dark))
        self.assertTrue(glass.isDark(0, dark))
        self.assertFalse(glass.isDark(0, light))

    def testStatusTextReadsAtFourAndAHalfToOneOnEachTheme(self):
        # the lightest panel the sidebar draws in the light theme, measured from a render, and each theme's own backdrop
        backgrounds = {glass.LIGHT: ("#F7FBF9", glass.LIGHT.base), glass.DARK: (glass.DARK.base,)}
        for theme, colors in backgrounds.items():
            for text in (theme.greenText, theme.redText):
                for background in colors:
                    self.assertGreaterEqual(contrast(text, background), 4.5, (theme.dark, text, background))

    def testTheGlowTurnsRedForARoughLowReading(self):
        class Mouse:
            reading = hidpp.BatteryReading(None, 0, level="low")
            asleep = False
        self.assertEqual(glass.stateColor(glass.DARK, Mouse(), False).name(), QColor(glass.DARK.red).name())

    def testTheGlowFollowsTheIconsOrder(self):
        theme = glass.DARK
        self.assertEqual(glass.stateColor(theme, None, False).name(), QColor(glass.GRAY).name())

        class Mouse:
            def __init__(self, percent, charging=0, asleep=False):
                self.reading = hidpp.BatteryReading(percent, charging)
                self.asleep = asleep
        self.assertEqual(glass.stateColor(theme, Mouse(15, asleep=True), True).name(), QColor(glass.GRAY).name())
        self.assertEqual(glass.stateColor(theme, Mouse(15, charging=1), True).name(), QColor(theme.green).name())
        self.assertEqual(glass.stateColor(theme, Mouse(15), True).name(), QColor(theme.red).name())
        self.assertEqual(glass.stateColor(theme, Mouse(60), True).name(), QColor(theme.orange).name())
        self.assertEqual(glass.stateColor(theme, Mouse(60), False).name(), QColor(theme.blue).name())

    def testAMouseInUseGlowsBlueAndAChargingOneGreen(self):
        class Mouse:
            def __init__(self, charging):
                self.reading = hidpp.BatteryReading(60, charging)
                self.asleep = False
        for theme, blue in ((glass.DARK, "#0A84FF"), (glass.LIGHT, "#007AFF")):
            self.assertEqual(theme.blue, blue)
            self.assertEqual(glass.stateColor(theme, Mouse(0), False).name(), QColor(blue).name())
            self.assertEqual(glass.stateColor(theme, Mouse(1), False).name(), QColor(theme.green).name())

    def testMotionFollowsKdesAnimationSpeed(self):
        folder = tempfile.mkdtemp()
        try:
            self.assertEqual(glass.animationFactor(folder, False), 1.0)
            with open(os.path.join(folder, "kdeglobals"), "w") as f:
                f.write("[KDE]\nAnimationDurationFactor=0.5\n")
            self.assertEqual(glass.animationFactor(folder, False), 0.5)
            self.assertEqual(glass.animationFactor(folder, True), 0.0)
            with open(os.path.join(folder, "kdeglobals"), "w") as f:
                f.write("[KDE]\nAnimationDurationFactor=0\n")
            self.assertEqual(glass.animationFactor(folder, False), 0.0)
        finally:
            shutil.rmtree(folder)

    def testTheEaseOutIsTheStrongCurve(self):
        self.assertEqual([round(glass.easeOut().valueForProgress(t), 3) for t in (0.1, 0.25, 0.5, 0.75, 1.0)], [0.398, 0.775, 0.966, 0.997, 1.0])

    def testAHugeAnimationSpeedIsCapped(self):
        # a hand edited factor this large would make a duration past a C int, which setDuration can't take
        folder = tempfile.mkdtemp()
        try:
            with open(os.path.join(folder, "kdeglobals"), "w") as f:
                f.write("[KDE]\nAnimationDurationFactor=1e8\n")
            factor = glass.animationFactor(folder, False)
            self.assertTrue(1.0 < factor <= 10.0, factor)
        finally:
            shutil.rmtree(folder)


class PaintTests(unittest.TestCase):
    def testTheBackdropIsTheBaseWithAGlowBehindTheIcon(self):
        image = glass.paintBackdrop(760, 560, QRectF(0, 0, 760, 560), QColor(glass.DARK.green), glass.DARK)
        # the bottom left corner is out of every glow's reach
        corner = image.pixelColor(0, 559)
        glow = image.pixelColor(150, 120)
        self.assertEqual(corner.name(), glass.DARK.base.lower())
        self.assertGreater(glow.green(), corner.green() + 40)

    def testTheGlowFollowsThePaneNotTheWindow(self):
        image = glass.paintBackdrop(992, 560, QRectF(232, 0, 760, 560), QColor(glass.DARK.green), glass.DARK)
        self.assertGreater(image.pixelColor(232 + 150, 120).green(), image.pixelColor(150, 120).green())

    def testTheFrostKeepsTheSizeAndBlurs(self):
        image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#000000"))
        painter = QPainter(image)
        painter.fillRect(0, 0, 120, 240, QColor("#FFFFFF"))
        painter.end()
        frost = glass.frosted(image)
        self.assertEqual((frost.width(), frost.height()), (240, 240))
        # the hard edge in the middle comes out a soft gray
        self.assertTrue(30 < frost.pixelColor(120, 120).red() < 225)

    def testAPanelPaintsItsWashAndRim(self):
        image = QImage(200, 120, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#000000"))
        frost = QImage(image)
        painter = QPainter(image)
        glass.paintPanel(painter, QRectF(20, 20, 160, 80), 18, frost, glass.DARK)
        painter.end()
        self.assertGreater(image.pixelColor(100, 60).red(), 0)
        self.assertGreater(image.pixelColor(100, 20).red(), image.pixelColor(100, 60).red())
        self.assertEqual(image.pixelColor(5, 5).red(), 0)


if (__name__ == "__main__"):
    unittest.main()
