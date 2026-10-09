from dataclasses import dataclass

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPixmap

# Qt's D-Bus tray drops any size over 64 px, so larger ones would only cost paint time
ICON_SIZES = [16, 22, 24, 32, 44, 48, 64]
# below this size the digits don't read, so the icon keeps its fill and drops the number
NUMBER_MIN_SIZE = 22
RED_PERCENT = 20
GREEN = "#30D158"
RED = "#FF453A"
GRAY = "#8E8E93"
# blocky digits drawn cell by cell, since a font at tray size let the hinter drop the tail that tells a 9 from an 8
DIGITS = {
    "0": ["####", "#..#", "#..#", "#..#", "#..#", "#..#", "####"],
    "1": ["..#.", ".##.", "..#.", "..#.", "..#.", "..#.", ".###"],
    "2": ["####", "...#", "...#", "####", "#...", "#...", "####"],
    "3": ["####", "...#", "...#", ".###", "...#", "...#", "####"],
    "4": ["#..#", "#..#", "#..#", "####", "...#", "...#", "...#"],
    "5": ["####", "#...", "#...", "####", "...#", "...#", "####"],
    "6": ["####", "#...", "#...", "####", "#..#", "#..#", "####"],
    "7": ["####", "...#", "...#", "..#.", "..#.", ".#..", ".#.."],
    "8": ["####", "#..#", "#..#", "####", "#..#", "#..#", "####"],
    "9": ["####", "#..#", "#..#", "####", "...#", "...#", "####"],
}
# 100 is the only three digit number, and it fits the body only in smaller digits, with a narrow 1 so it centers on a whole pixel
SMALL_DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#", "##", ".#", ".#", ".#"],
}


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
    # at 5% the red fill is a single row, so the number turns red with it
    if (not (state.asleep or state.empty or state.charging) and state.percent is not None and state.percent <= RED_PERCENT):
        return QColor(RED)
    return markColor(state, baseColor)


def roundedPath(x, y, width, height, radius, k):
    path = QPainterPath()
    path.addRoundedRect(QRectF(x * k, y * k, width * k, height * k), radius * k, radius * k)
    return path


def drawWheel(painter, k, color):
    painter.fillPath(roundedPath(10.25, 3.2, 1.5, 3.4, 0.75, k), color)


def numberLayout(text, size):
    # whole pixel cells keep the digits sharp, and the plate is the box one cell outside the digits that their outline stays within
    glyphs = SMALL_DIGITS if len(text) >= 3 else DIGITS
    rows = len(glyphs["0"])
    k = size / 22
    unit = max(1, round(k))
    columns = sum(len(glyphs[digit][0]) for digit in text) + len(text) - 1
    width = columns * unit
    left = round(11 * k) - width // 2
    top = round(14 * k) - rows * unit // 2
    cells = []
    x = left
    for digit in text:
        for row, line in enumerate(glyphs[digit]):
            for column, mark in enumerate(line):
                if (mark == "#"):
                    cells.append(QRect(x + column * unit, top + row * unit, unit, unit))
        x += (len(glyphs[digit][0]) + 1) * unit
    plate = QRect(left - unit, top - unit, width + 2 * unit, (rows + 2) * unit)
    return cells, plate


def outlineRects(cells):
    # each lit cell grown by one cell on every side, so the cleared area follows the digits instead of boxing them
    unit = cells[0].width() if cells else 0
    return [cell.adjusted(-unit, -unit, unit, unit) for cell in cells]


def drawNumber(painter, state, size, baseColor):
    if (state.percent is None or size < NUMBER_MIN_SIZE):
        return
    cells, plate = numberLayout(str(max(0, min(100, state.percent))), size)
    # the outline clears the fill and track around each digit, so the fill line never runs through one
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    for rect in outlineRects(cells):
        painter.fillRect(rect, QColor(0, 0, 0))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    color = numberColor(state, baseColor)
    for cell in cells:
        painter.fillRect(cell, color)


def renderImage(state, size, baseColor, number=True):
    k = size / 22
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    body = roundedPath(4, 1, 14, 20, 7, k)
    color = markColor(state, baseColor)
    track = QColor(color)
    track.setAlphaF(0.3 if (state.asleep or state.empty) else 0.25)
    painter.fillPath(body, track)

    if (state.empty):
        drawWheel(painter, k, color)
        painter.end()
        return image

    level = 100 if state.percent is None else max(0, min(100, state.percent))
    fillTop = round((21 - 20 * level / 100) * k)

    painter.save()
    painter.setClipRect(QRectF(0, fillTop, size, size - fillTop))
    painter.fillPath(body, fillColor(state, baseColor))
    # the wheel becomes a hole in the fill, letting the panel show through
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    drawWheel(painter, k, QColor(0, 0, 0))
    painter.restore()

    painter.save()
    painter.setClipRect(QRectF(0, 0, size, fillTop))
    drawWheel(painter, k, color)
    painter.restore()

    # the battery window draws the mouse large beside its own percent, so it leaves the number out
    if (number):
        drawNumber(painter, state, size, baseColor)

    painter.end()
    return image


def makeIcon(state, baseColor):
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(renderImage(state, size, baseColor)))
    return icon
