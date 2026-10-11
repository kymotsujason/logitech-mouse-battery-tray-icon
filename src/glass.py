import math
import os
from dataclasses import dataclass

from PyQt6.QtCore import QEasingCurve, QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QRadialGradient

import hidpp
import icon
import panelColor

DESIGN_WIDTH = 760
DESIGN_HEIGHT = 560
FROST_SCALE = 24
BLUE = "#0A84FF"
GRAY = "#8E8E93"
SHEEN_HEIGHT = 60
# the three glows in a 760 by 560 pane, as (x, y, radius, alpha in dark, alpha in light, which color)
GLOWS = ((150, 120, 330, 0.42, 0.38, "state"), (700, 520, 420, 0.16, 0.14, "blue"), (620, 60, 260, 0.10, 0.10, "state"))
SHADOWS = ((10, 0.05), (6, 0.07), (3, 0.09))
SHADOW_DROP = 6
# a hand edited factor of a few million would make a duration past the C int setDuration takes, so motion goes no slower than ten times
MAX_ANIMATION_FACTOR = 10.0


@dataclass(frozen=True)
class Theme:
    dark: bool
    base: str
    wash: float
    strongWash: float
    rim: tuple
    shadow: float
    text: str
    secondary: str
    grid: tuple
    bar: tuple
    green: str
    red: str
    orange: str
    iconBase: str
    # small status text needs 4.5 to 1 against the panels, which the light theme's bright green and red don't reach
    greenText: str
    redText: str
    blue: str
    chargeSeries: tuple


DARK = Theme(True, "#0E1012", 0.07, 0.13, (0.32, 0.04, 0.06), 1.0, "#FFFFFF", "#A1A1A6", (255, 255, 255, 0.07), (255, 255, 255, 0.30), "#30D158", "#FF453A", "#FF9F0A", "#F5F5F7", "#30D158", "#FF453A", "#0A84FF", ("#24A957", "#5E5CE6"))
LIGHT = Theme(False, "#E9EBEE", 0.55, 0.85, (0.95, 0.35, 0.56), 0.25, "#1D1D1F", "#6E6E73", (0, 0, 0, 0.07), (0, 0, 0, 0.22), "#34C759", "#FF3B30", "#FF9500", "#1D1D1F", "#197A35", "#D70015", "#007AFF", ("#248A3D", "#5856D6"))


def rgba(values):
    red, green, blue, alpha = values
    color = QColor(red, green, blue)
    color.setAlphaF(alpha)
    return color


def isDark(portalScheme, palette):
    # the portal's choice wins, and with none the app palette's window color decides
    if (portalScheme == panelColor.PREFER_DARK):
        return True
    if (portalScheme == panelColor.PREFER_LIGHT):
        return False
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def themeFor(dark):
    return DARK if dark else LIGHT


def stateColor(theme, mouse, warning):
    # the icon's own order, with the health box's warning added before the green
    if (mouse is None or mouse.reading is None or mouse.asleep):
        return QColor(GRAY)
    reading = mouse.reading
    if (reading.charging in hidpp.EXTERNAL_POWER):
        return QColor(theme.green)
    percent = hidpp.estimatedPercent(reading)
    if (percent is not None and percent <= icon.RED_PERCENT):
        return QColor(theme.red)
    if (warning):
        return QColor(theme.orange)
    return QColor(theme.blue)


def animationFactor(configHome, reducedMotion):
    if (reducedMotion):
        return 0.0
    groups = panelColor.readIni(os.path.join(configHome, "kdeglobals")) or {}
    try:
        factor = float(groups.get("KDE", {}).get("AnimationDurationFactor", "1.0"))
    except ValueError:
        return 1.0
    return min(MAX_ANIMATION_FACTOR, max(0.0, factor)) if math.isfinite(factor) else 1.0


def easeOut():
    # cubic-bezier(0.23, 1, 0.32, 1), a strong ease out that moves at once and settles slowly
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.23, 1.0), QPointF(0.32, 1.0), QPointF(1.0, 1.0))
    return curve


def fadeCurve():
    # opacity on the strong ease out is mostly there after a frame or two, so a fade reads as a cut, while this one spreads the change over the whole time
    return QEasingCurve(QEasingCurve.Type.InOutQuad)


def paintBackdrop(width, height, pane, color, theme, ratio=1.0):
    image = QImage(max(1, round(width * ratio)), max(1, round(height * ratio)), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(QColor(theme.base))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # the glows sit in the main pane and scale with it, so the sidebar never moves them
    scaleX = pane.width() / DESIGN_WIDTH
    scaleY = pane.height() / DESIGN_HEIGHT
    for x, y, radius, darkAlpha, lightAlpha, which in GLOWS:
        glow = QColor(color if which == "state" else QColor(BLUE))
        center = QPointF(pane.x() + x * scaleX, pane.y() + y * scaleY)
        gradient = QRadialGradient(center, radius * scaleX)
        inner = QColor(glow)
        inner.setAlphaF(darkAlpha if theme.dark else lightAlpha)
        outer = QColor(glow)
        outer.setAlphaF(0.0)
        gradient.setColorAt(0, inner)
        gradient.setColorAt(1, outer)
        painter.fillRect(QRectF(0, 0, width, height), QBrush(gradient))
    painter.end()
    return image


def frosted(image):
    # scaling far down and back up smoothly is a wide blur, made once and kept until the size or the glow changes
    small = image.scaled(max(1, image.width() // FROST_SCALE), max(1, image.height() // FROST_SCALE), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    frost = small.scaled(image.width(), image.height(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    frost.setDevicePixelRatio(image.devicePixelRatio())
    return frost


def roundedPath(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def paintShadow(painter, rect, radius, theme):
    for spread, alpha in SHADOWS:
        shade = QColor(0, 0, 0)
        shade.setAlphaF(alpha * theme.shadow)
        painter.fillPath(roundedPath(rect.adjusted(-spread, -spread + SHADOW_DROP, spread, spread + SHADOW_DROP), radius + spread), shade)


def paintPanel(painter, rect, radius, frost, theme, strong=False, offset=QPointF(0, 0)):
    # rect is in the painter's coordinates, and offset is where those sit in the window, so the frost lines up with the backdrop behind
    path = roundedPath(rect, radius)
    painter.save()
    painter.setClipPath(path)
    ratio = frost.devicePixelRatio()
    source = QRectF((rect.x() + offset.x()) * ratio, (rect.y() + offset.y()) * ratio, rect.width() * ratio, rect.height() * ratio)
    painter.drawImage(rect, frost, source)
    wash = QColor(255, 255, 255)
    wash.setAlphaF(theme.strongWash if strong else theme.wash)
    painter.fillPath(path, wash)
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.x(), rect.y() + min(SHEEN_HEIGHT, rect.height())))
    sheen.setColorAt(0, rgba((255, 255, 255, 0.08)))
    sheen.setColorAt(1, rgba((255, 255, 255, 0.0)))
    painter.fillPath(path, QBrush(sheen))
    painter.restore()
    top, middle, bottom = theme.rim
    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    rim.setColorAt(0, rgba((255, 255, 255, top)))
    rim.setColorAt(0.5, rgba((255, 255, 255, middle)))
    rim.setColorAt(1, rgba((255, 255, 255, bottom)))
    painter.setPen(QPen(QBrush(rim), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
