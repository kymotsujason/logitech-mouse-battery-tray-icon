import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

import glass

LEVEL_EMPTY = "No readings in the last 7 days"
HOURS_EMPTY = "No measured drain yet"
CHARGING_EMPTY = "No measured charges yet"
HOURS_CAPTION = "A rough guide, since each month mixes every part of the battery. The health box is the one to trust."
# a rough level has no percent, so the chart draws it at a fixed height
LEVEL_HEIGHTS = {"full": 100, "high": 85, "good": 70, "normal": 55, "low": 25, "critical": 10}
LABEL_SIZE = 10
TITLE_SIZE = 12
AXIS_WIDTH = 44
TOP = 30
BOTTOM = 24
MONTH_LABEL_WIDTH = 40


def font(size, weight=QFont.Weight.Normal):
    chosen = QFont()
    chosen.setPixelSize(size)
    chosen.setWeight(weight)
    return chosen


def labeledMonths(places):
    # walked from the latest month, so the newest one always has its name
    picked = []
    for index in reversed(range(len(places))):
        if (not picked or places[picked[-1]] - places[index] >= MONTH_LABEL_WIDTH):
            picked.append(index)
    return list(reversed(picked))


def latestText(value, unit, month, now):
    if (month == time.strftime("%Y-%m", time.localtime(now))):
        return str(round(value)) + " " + unit + ", this month"
    return str(round(value)) + " " + unit + ", " + time.strftime("%b %Y", time.strptime(month, "%Y-%m"))


def slotValue(slot):
    endAt, percent, level, millivolts, charged = slot
    if (percent is not None):
        return percent
    if (level is not None):
        return LEVEL_HEIGHTS.get(level)
    return millivolts


class Chart(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.theme = glass.DARK
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def setTheme(self, theme):
        self.theme = theme
        self.update()

    def area(self):
        return QRectF(16, TOP, self.width() - 32 - AXIS_WIDTH, self.height() - TOP - BOTTOM)

    def paintTitle(self, painter):
        painter.setFont(font(TITLE_SIZE, QFont.Weight.Medium))
        painter.setPen(QColor(self.theme.secondary))
        painter.drawText(QRectF(16, 6, self.width() - 32, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.title)

    def paintEmpty(self, painter, text):
        painter.setFont(font(13))
        painter.setPen(QColor(self.theme.secondary))
        painter.drawText(QRectF(0, 0, self.width(), self.height()), int(Qt.AlignmentFlag.AlignCenter), text)

    def paintGrid(self, painter, area, labels):
        for index, label in enumerate(labels):
            y = area.y() + area.height() * index / max(1, len(labels) - 1)
            painter.setPen(QPen(glass.rgba(self.theme.grid), 1))
            painter.drawLine(QPointF(area.x(), y), QPointF(area.right(), y))
            painter.setFont(font(LABEL_SIZE))
            painter.setPen(QColor(self.theme.secondary))
            painter.drawText(QRectF(area.right() + 8, y - 8, AXIS_WIDTH, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)


class LevelChart(Chart):
    def __init__(self, parent=None):
        super().__init__("Battery Level, Last 7 Days", parent)
        self.slots = []
        self.voltage = False
        self.hover = None
        self.setMouseTracking(True)

    def setSlots(self, slots, voltage=False):
        self.slots = slots
        self.voltage = voltage
        self.hover = None
        self.update()

    def scale(self):
        if (not self.voltage):
            return (0, 100, ["100%", "50%", "0%"])
        values = [slot[3] for slot in self.slots if slot[3] is not None]
        low, high = (min(values), max(values)) if values else (0, 1)
        if (high == low):
            high = low + 1
        return (low, high, [str(high) + " mV", str((low + high) // 2) + " mV", str(low) + " mV"])

    def barAt(self, x):
        area = self.area()
        if (not self.slots or x < area.x() or x > area.right()):
            return None
        return min(len(self.slots) - 1, int((x - area.x()) / area.width() * len(self.slots)))

    def mouseMoveEvent(self, event):
        index = self.barAt(event.position().x())
        if (index != self.hover):
            self.hover = index
            self.update()

    def leaveEvent(self, event):
        self.hover = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paintTitle(painter)
        if (not any(slotValue(slot) is not None for slot in self.slots)):
            self.paintEmpty(painter, LEVEL_EMPTY)
            return
        area = self.area()
        low, high, labels = self.scale()
        self.paintGrid(painter, area, labels)
        gap = 2.0
        width = (area.width() - gap * (len(self.slots) - 1)) / len(self.slots)
        for index, slot in enumerate(self.slots):
            value = slotValue(slot)
            if (value is None):
                continue
            share = (value - low) / (high - low) if self.voltage else value / 100
            height = max(1.0, area.height() * min(1.0, max(0.0, share)))
            rect = QRectF(area.x() + index * (width + gap), area.bottom() - height, width, height)
            path = QPainterPath()
            path.addRoundedRect(rect, 1.5, 1.5)
            if (slot[4]):
                fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                fill.setColorAt(0, QColor(self.theme.green).lighter(125))
                fill.setColorAt(1, QColor(self.theme.green))
                painter.fillPath(path, QBrush(fill))
            else:
                color = glass.rgba(self.theme.bar)
                if (index == self.hover):
                    color.setAlphaF(min(1.0, color.alphaF() * 2))
                painter.fillPath(path, color)
        self.paintDays(painter, area)
        self.paintHover(painter, area)

    def paintDays(self, painter, area):
        painter.setFont(font(LABEL_SIZE))
        painter.setPen(QColor(self.theme.secondary))
        lastDay = None
        lastX = None
        for index, slot in enumerate(self.slots):
            # a slot's day is the day its two hours started in
            day = time.strftime("%a", time.localtime(slot[0] - 2 * 60 * 60))
            x = area.x() + area.width() * index / len(self.slots)
            if (day != lastDay and (lastX is None or x - lastX >= 40)):
                painter.drawText(QRectF(x, area.bottom() + 6, 60, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), day)
                lastX = x
            lastDay = day

    def paintHover(self, painter, area):
        if (self.hover is None):
            return
        slot = self.slots[self.hover]
        value = slotValue(slot)
        if (value is None):
            return
        when = time.strftime("%a %H:%M", time.localtime(slot[0]))
        if (self.voltage):
            text = when + ", " + str(slot[3]) + " mV"
        elif (slot[1] is None):
            text = when + ", " + str(slot[2])
        else:
            text = when + ", " + str(slot[1]) + "%"
        painter.setFont(font(LABEL_SIZE, QFont.Weight.Medium))
        painter.setPen(QColor(self.theme.text))
        painter.drawText(QRectF(area.x(), 6, area.width() + AXIS_WIDTH, 20), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text)


class LineChart(Chart):
    def __init__(self, title, unit, empty, caption=None, parent=None):
        super().__init__(title, parent)
        self.unit = unit
        self.empty = empty
        self.caption = caption
        self.series = {}

    def setSeries(self, series):
        self.series = {label: points for label, points in series.items() if points}
        self.update()

    def months(self):
        return sorted({month for points in self.series.values() for month, value in points})

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paintTitle(painter)
        if (not self.series):
            self.paintEmpty(painter, self.empty)
            return
        months = self.months()
        values = [value for points in self.series.values() for month, value in points]
        low, high = min(values) * 0.8, max(values) * 1.2
        if (high <= low):
            high = low + 1
        area = self.area().adjusted(0, 22, 0, -16 if self.caption else 0)
        self.paintGrid(painter, area, [str(round(high)) + " " + self.unit, str(round((low + high) / 2)) + " " + self.unit, str(round(low)) + " " + self.unit])
        colors = [QColor(self.theme.green), QColor(glass.BLUE), QColor(self.theme.orange), QColor(self.theme.secondary)]
        legend = []
        for (label, points), color in zip(sorted(self.series.items()), colors):
            positions = [QPointF(area.x() + area.width() * (months.index(month) / max(1, len(months) - 1) if len(months) > 1 else 0.5), area.bottom() - (value - low) / (high - low) * area.height()) for month, value in points]
            self.paintLine(painter, area, positions, color, fade=len(self.series) == 1)
            legend.append((label, color))
        latestMonth, latest = self.series[sorted(self.series)[0]][-1]
        painter.setFont(font(22, QFont.Weight.Bold))
        painter.setPen(QColor(self.theme.text))
        painter.drawText(QRectF(16, TOP - 2, self.width() - 32 - AXIS_WIDTH, 26), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), latestText(latest, self.unit, latestMonth, time.time()))
        if (len(legend) > 1):
            self.paintLegend(painter, legend)
        self.paintMonths(painter, area, months)
        if (self.caption):
            painter.setFont(font(LABEL_SIZE))
            painter.setPen(QColor(self.theme.secondary))
            painter.drawText(QRectF(16, self.height() - 18, self.width() - 32, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.caption)

    def paintLine(self, painter, area, positions, color, fade):
        path = QPainterPath()
        path.moveTo(positions[0])
        for start, end in zip(positions, positions[1:]):
            middle = (start.x() + end.x()) / 2
            path.cubicTo(QPointF(middle, start.y()), QPointF(middle, end.y()), end)
        if (fade and len(positions) > 1):
            fill = QPainterPath(path)
            fill.lineTo(positions[-1].x(), area.bottom())
            fill.lineTo(positions[0].x(), area.bottom())
            gradient = QLinearGradient(0, area.y(), 0, area.bottom())
            top = QColor(color)
            top.setAlphaF(0.28)
            bottom = QColor(color)
            bottom.setAlphaF(0.0)
            gradient.setColorAt(0, top)
            gradient.setColorAt(1, bottom)
            painter.fillPath(fill, QBrush(gradient))
        painter.setPen(QPen(color, 2.5, cap=Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if (len(positions) == 1):
            painter.setBrush(color)
            painter.drawEllipse(positions[0], 3, 3)

    def paintLegend(self, painter, legend):
        painter.setFont(font(LABEL_SIZE))
        x = self.width() - 16 - AXIS_WIDTH
        for label, color in reversed(legend):
            width = painter.fontMetrics().horizontalAdvance(label) + 18
            x -= width
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x + 5, 16), 4, 4)
            painter.setPen(QColor(self.theme.secondary))
            painter.drawText(QRectF(x + 12, 8, width, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)

    def paintMonths(self, painter, area, months):
        painter.setFont(font(LABEL_SIZE))
        painter.setPen(QColor(self.theme.secondary))
        places = [area.x() + area.width() * (index / max(1, len(months) - 1) if len(months) > 1 else 0.5) for index in range(len(months))]
        for index in labeledMonths(places):
            name = time.strftime("%b", time.strptime(months[index], "%Y-%m"))
            painter.drawText(QRectF(places[index] - MONTH_LABEL_WIDTH / 2, area.bottom() + 6, MONTH_LABEL_WIDTH, 16), int(Qt.AlignmentFlag.AlignCenter), name)
