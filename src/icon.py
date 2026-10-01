import os
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QIcon, QImage, QPainter, QPainterPath, QPixmap

ICON_SIZES = [16, 22, 24, 32, 44, 48, 64, 96, 128]
# below this size the digits don't read, thus the icon keeps its fill and drops the number
NUMBER_MIN_SIZE = 22
# up to this size antialiased digits read faint, thus they're drawn crisp
CRISP_MAX_SIZE = 22
RED_PERCENT = 20
GREEN = "#30D158"
RED = "#FF453A"
GRAY = "#8E8E93"
FALLBACK_FAMILY = "Noto Sans"
FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NotoSans-Medium.ttf")
digitFamilyName = None


def digitFamily():
    global digitFamilyName
    if (digitFamilyName is None):
        digitFamilyName = FALLBACK_FAMILY
        fontId = QFontDatabase.addApplicationFont(FONT_FILE)
        if (fontId >= 0):
            families = QFontDatabase.applicationFontFamilies(fontId)
            if (families):
                digitFamilyName = families[0]
    return digitFamilyName


@dataclass(frozen=True)
class IconState:
    percent: int | None = None
    charging: bool = False
    asleep: bool = False
    empty: bool = False


def fillColor(state, baseColor):
    if (state.asleep or state.empty):
        return QColor(GRAY)
    if (state.charging):
        return QColor(GREEN)
    if (state.percent is not None and state.percent <= RED_PERCENT):
        return QColor(RED)
    return QColor(baseColor)


def markColor(state, baseColor):
    if (state.asleep or state.empty):
        return QColor(GRAY)
    return QColor(baseColor)


def numberColor(state, baseColor):
    # at 5% the red fill is a single row, thus the number turns red with it
    if (not (state.asleep or state.empty or state.charging) and state.percent is not None and state.percent <= RED_PERCENT):
        return QColor(RED)
    return markColor(state, baseColor)


def roundedPath(x, y, width, height, radius, k):
    path = QPainterPath()
    path.addRoundedRect(QRectF(x * k, y * k, width * k, height * k), radius * k, radius * k)
    return path


def drawMarks(painter, state, k, color, textColor):
    painter.fillPath(roundedPath(10.25, 3.2, 1.5, 3.4, 0.75, k), color)
    size = round(22 * k)
    if (state.empty or state.percent is None or size < NUMBER_MIN_SIZE):
        return
    text = str(state.percent)
    pixelSize = (6.5 if len(text) >= 3 else 8) * k
    font = QFont(digitFamily())
    font.setWeight(QFont.Weight.Medium)
    if (size <= CRISP_MAX_SIZE):
        # antialiased digits at tray size only partly cover their pixels and read faint, thus draw them crisp
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    font.setPointSizeF(pixelSize * 72.0 / painter.device().logicalDpiY())
    bounds = QFontMetricsF(font, painter.device()).tightBoundingRect(text)
    # whole pixel positions keep the hinted digits sharp
    x = round(11 * k - (bounds.left() + bounds.width() / 2))
    y = round(14 * k - (bounds.top() + bounds.height() / 2))
    painter.setFont(font)
    painter.setPen(textColor)
    painter.drawText(QPointF(x, y), text)


def renderImage(state, size, baseColor):
    k = size / 22
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    body = roundedPath(4, 1, 14, 20, 7, k)
    color = markColor(state, baseColor)
    track = QColor(color)
    track.setAlphaF(0.3 if (state.asleep or state.empty) else 0.25)
    painter.fillPath(body, track)

    if (state.empty):
        drawMarks(painter, state, k, color, color)
        painter.end()
        return image

    level = 100 if state.percent is None else max(0, min(100, state.percent))
    fillTop = round((21 - 20 * level / 100) * k)

    painter.save()
    painter.setClipRect(QRectF(0, fillTop, size, size - fillTop))
    painter.fillPath(body, fillColor(state, baseColor))
    # the number and wheel become holes in the fill, letting the panel show through
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    drawMarks(painter, state, k, QColor(0, 0, 0), QColor(0, 0, 0))
    painter.restore()

    painter.save()
    painter.setClipRect(QRectF(0, 0, size, fillTop))
    drawMarks(painter, state, k, color, numberColor(state, baseColor))
    painter.restore()

    painter.end()
    return image


def makeIcon(state, baseColor):
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(renderImage(state, size, baseColor)))
    return icon
