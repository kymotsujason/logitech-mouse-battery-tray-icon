import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

import glass
import hidpp
import mice

RANGE_TITLES = {"day": "Battery Level, Last 24 Hours", "week": "Battery Level, Last 7 Days", "month": "Battery Level, Last 30 Days", "halfYear": "Battery Level, Last 6 Months", "year": "Battery Level, Last Year", "all": "Battery Level, All Time"}
RANGE_EMPTY = {"day": "No readings in the last 24 hours", "week": "No readings in the last 7 days", "month": "No readings in the last 30 days", "halfYear": "No readings in the last 6 months", "year": "No readings in the last year", "all": "No readings yet"}
# the same slot lengths as history.LEVEL_RANGES, where None is a calendar month
RANGE_SECONDS = {"day": 30 * 60, "week": 2 * 60 * 60, "month": 12 * 60 * 60, "halfYear": 7 * 24 * 60 * 60, "year": 7 * 24 * 60 * 60, "all": None}
SPAN_RANGES = ("month", "halfYear", "year", "all")
LABEL_SPACING = {"day": 40, "week": 40, "month": 64, "halfYear": 36, "year": 36, "all": 36}
LEVEL_EMPTY = RANGE_EMPTY["week"]
MAX_BAR_WIDTH = 28
MIN_BAR_HEIGHT = 3
BAR_GAP = 2.0
RANGE_ROOM = 10
HOURS_EMPTY = "No measured drain yet"
CHARGING_EMPTY = "No measured charges yet"
HOURS_CAPTION = "A rough guide, since each month mixes every part of the battery. The health box is the one to trust."
LEVEL_HEIGHTS = hidpp.LEVEL_HEIGHTS
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
    # the service groups the figures by UTC month, so this month is the UTC one too
    if (month == time.strftime("%Y-%m", time.gmtime(now))):
        return str(round(value)) + " " + unit + ", this month"
    named = time.strptime(month, "%Y-%m")
    return str(round(value)) + " " + unit + ", " + mice.MONTH_NAMES[named.tm_mon - 1] + " " + str(named.tm_year)


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
        # the chart is painted, so its title is its name for a screen reader
        self.setAccessibleName(title)
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
        super().__init__(RANGE_TITLES["week"], parent)
        self.slots = []
        self.voltage = False
        self.rough = False
        self.rangeName = "week"
        self.message = None
        self.hover = None
        self.morphMs = 0
        self.fromRects = None
        self.morphProgress = 1.0
        self.morph = QVariantAnimation(self)
        self.morph.setEasingCurve(glass.easeOut())
        self.morph.setStartValue(0.0)
        self.morph.setEndValue(1.0)
        self.morph.valueChanged.connect(self.onMorphStep)
        self.setMouseTracking(True)

    def onMorphStep(self, value):
        self.morphProgress = value
        self.update()

    def shownRect(self, index):
        target = self.barRect(index)
        if (target is None or self.fromRects is None or self.morphProgress >= 1.0):
            return target
        start = self.fromRects[index]
        if (start is None):
            start = QRectF(target.x(), target.bottom(), target.width(), 0.0)
        p = self.morphProgress
        return QRectF(start.x() + (target.x() - start.x()) * p, start.y() + (target.y() - start.y()) * p, start.width() + (target.width() - start.width()) * p, start.height() + (target.height() - start.height()) * p)

    def setSlots(self, slots, voltage=False, rangeName="week", message=None, rough=False):
        # every state change sets the slots again, so the bar under the pointer keeps its readout until the range changes, a message takes the bars' place, or the bar is gone
        if (rangeName != self.rangeName or message is not None or (self.hover is not None and self.hover >= len(slots))):
            self.hover = None
        if (self.isVisible() and rangeName == self.rangeName and message is None and self.message is None and slots == self.slots and voltage == self.voltage and rough == self.rough):
            # nothing changed, so a morph still running carries on
            self.update()
            return
        morphs = self.morphMs > 0 and self.isVisible() and rangeName == self.rangeName and message is None and self.message is None and len(slots) == len(self.slots) and slots != self.slots
        # taken before the slots change, so a morph in progress carries on from what's drawn
        rects = [self.shownRect(index) for index in range(len(self.slots))] if morphs else None
        self.morph.stop()
        self.slots = slots
        self.voltage = voltage
        self.rough = rough
        self.rangeName = rangeName
        self.title = RANGE_TITLES[rangeName]
        self.setAccessibleName(self.title)
        # message stands in for the bars while a range loads or can't be read
        self.message = message
        self.fromRects = rects
        self.morphProgress = 0.0 if morphs else 1.0
        if (morphs):
            self.morph.setDuration(self.morphMs)
            self.morph.start()
        self.update()

    def isSpan(self):
        return self.rangeName in SPAN_RANGES

    def emptyText(self):
        return self.message or RANGE_EMPTY[self.rangeName]

    def area(self):
        # the range control sits in the top right corner, so the bars start below it
        return QRectF(16, TOP + RANGE_ROOM, self.width() - 32 - AXIS_WIDTH, self.height() - TOP - RANGE_ROOM - BOTTOM)

    def values(self, slot):
        if (self.isSpan()):
            return [] if slot[1] is None else [slot[1], slot[2]]
        value = slotValue(slot)
        return [] if value is None else [value]

    def scale(self):
        if (not self.voltage):
            return (0, 100, ["100%", "50%", "0%"])
        values = [value for slot in self.slots for value in self.values(slot)]
        low, high = (min(values), max(values)) if values else (0, 1)
        if (high == low):
            high = low + 1
        return (low, high, [str(high) + " mV", str((low + high) // 2) + " mV", str(low) + " mV"])

    def heightOf(self, value, low, high):
        share = (value - low) / (high - low) if self.voltage else value / 100
        return self.area().height() * min(1.0, max(0.0, share))

    def barRect(self, index):
        values = self.values(self.slots[index])
        if (not values):
            return None
        area = self.area()
        low, high, labels = self.scale()
        slotWidth = area.width() / len(self.slots)
        width = min(MAX_BAR_WIDTH, slotWidth - BAR_GAP if len(self.slots) > 1 else slotWidth)
        x = area.x() + index * slotWidth + (slotWidth - width) / 2
        top = area.bottom() - self.heightOf(max(values), low, high)
        bottom = area.bottom() - (self.heightOf(min(values), low, high) if self.isSpan() else 0)
        if (bottom - top < MIN_BAR_HEIGHT):
            # a level that held all slot long is a thin bar instead of nothing
            middle = (top + bottom) / 2
            top, bottom = middle - MIN_BAR_HEIGHT / 2, middle + MIN_BAR_HEIGHT / 2
            if (not self.isSpan()):
                top, bottom = area.bottom() - MIN_BAR_HEIGHT / 3, area.bottom()
        return QRectF(x, top, width, bottom - top)

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

    def slotStart(self, index):
        end = self.slots[index][0]
        seconds = RANGE_SECONDS[self.rangeName]
        # a month slot starts on the 1st, so a moment just before its end names its month
        return end - 1 if seconds is None else end - seconds

    def axisLabels(self):
        labels = []
        lastKey = None
        lastX = None
        area = self.area()
        byYear = self.rangeName == "all" and len(self.slots) > 24
        for index in range(len(self.slots)):
            moment = time.localtime(self.slotStart(index))
            if (self.rangeName == "day"):
                key, label = (moment.tm_yday, moment.tm_hour // 6), "%02d:00" % (moment.tm_hour // 6 * 6)
            elif (self.rangeName == "week"):
                key, label = moment.tm_yday, mice.DAY_NAMES[moment.tm_wday]
            elif (self.rangeName == "month"):
                key, label = moment.tm_yday, mice.MONTH_NAMES[moment.tm_mon - 1] + " " + str(moment.tm_mday)
            elif (byYear):
                key, label = moment.tm_year, str(moment.tm_year)
            else:
                # January is named by its year, so a chart over a year apart shows which October is which
                key, label = (moment.tm_year, moment.tm_mon), str(moment.tm_year) if moment.tm_mon == 1 else mice.MONTH_NAMES[moment.tm_mon - 1]
            x = self.labelCenter(index) if self.centersLabels() else area.x() + area.width() * index / len(self.slots)
            # a slot of its own month is named whole, while any other first slot starts partway through its day or month, so its name waits for the next one
            first = lastKey is None and not self.centersLabels()
            if (not first and key != lastKey and (lastX is None or x - lastX >= LABEL_SPACING[self.rangeName])):
                labels.append((index, label))
                lastX = x
            lastKey = key
        return labels

    def centersLabels(self):
        # each month of All is one bar, so its name sits under that bar
        return self.rangeName == "all" and len(self.slots) <= 24

    def labelCenter(self, index):
        area = self.area()
        slotWidth = area.width() / len(self.slots)
        return area.x() + (index + 0.5) * slotWidth

    def valueText(self, value):
        if (self.voltage):
            return str(value) + " mV"
        if (self.rough):
            # a rough level's slot holds the heights it was drawn at, so they're named again
            return next((name for name, height in LEVEL_HEIGHTS.items() if height == value), str(value))
        return str(value) + "%"

    def hoverText(self, index):
        slot = self.slots[index]
        start = self.slotStart(index)
        if (not self.isSpan()):
            moment = time.localtime(slot[0])
            when = mice.DAY_NAMES[moment.tm_wday] + time.strftime(" %H:%M", moment)
            if (self.voltage):
                return when + ", " + str(slot[3]) + " mV"
            if (slot[1] is None):
                return when + ", " + str(slot[2])
            return when + ", " + str(slot[1]) + "%"
        begin = time.localtime(start)
        if (self.rangeName == "month"):
            when = mice.MONTH_NAMES[begin.tm_mon - 1] + " " + str(begin.tm_mday) + time.strftime(", %H:%M to ", begin) + time.strftime("%H:%M", time.localtime(slot[0]))
        elif (self.rangeName == "all"):
            when = mice.FULL_MONTH_NAMES[begin.tm_mon - 1] + " " + str(begin.tm_year)
        else:
            when = "Week of " + mice.MONTH_NAMES[begin.tm_mon - 1] + " " + str(begin.tm_mday)
        if (slot[1] is None):
            return when + ", no readings"
        levels = self.valueText(slot[1]) if slot[1] == slot[2] else self.valueText(slot[1]) + " to " + self.valueText(slot[2])
        charges = "" if slot[3] == 0 else (", charged" if slot[3] == 1 else ", charged " + str(slot[3]) + " times")
        return when + ", " + levels + charges

    def paintTitle(self, painter):
        # the hovered bar's text takes the title's place, since the range control holds the top right corner
        if (self.hover is None or not self.values(self.slots[self.hover])):
            super().paintTitle(painter)
            return
        painter.setFont(font(TITLE_SIZE, QFont.Weight.Medium))
        painter.setPen(QColor(self.theme.text))
        painter.drawText(QRectF(16, 6, self.width() - 32, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.hoverText(self.hover))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paintTitle(painter)
        if (self.message is not None or not any(self.values(slot) for slot in self.slots)):
            self.paintEmpty(painter, self.emptyText())
            return
        area = self.area()
        low, high, labels = self.scale()
        self.paintGrid(painter, area, labels)
        for index, slot in enumerate(self.slots):
            rect = self.shownRect(index)
            if (rect is None):
                continue
            path = QPainterPath()
            path.addRoundedRect(rect, min(1.5, rect.width() / 2), min(1.5, rect.width() / 2))
            # a long slot nearly always holds a charge, so only the short ranges mark it in green
            if (not self.isSpan() and slot[4]):
                fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                fill.setColorAt(0, QColor(self.theme.green).lighter(125))
                fill.setColorAt(1, QColor(self.theme.green))
                painter.fillPath(path, QBrush(fill))
            else:
                color = glass.rgba(self.theme.bar)
                if (index == self.hover):
                    color.setAlphaF(min(1.0, color.alphaF() * 2))
                painter.fillPath(path, color)
        self.paintLabels(painter, area)

    def paintLabels(self, painter, area):
        painter.setFont(font(LABEL_SIZE))
        painter.setPen(QColor(self.theme.secondary))
        for index, label in self.axisLabels():
            if (self.centersLabels()):
                painter.drawText(QRectF(self.labelCenter(index) - 30, area.bottom() + 6, 60, 16), int(Qt.AlignmentFlag.AlignCenter), label)
                continue
            x = area.x() + area.width() * index / len(self.slots)
            painter.drawText(QRectF(x, area.bottom() + 6, 60, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)


class LineChart(Chart):
    def __init__(self, title, unit, empty, caption=None, parent=None):
        # these charts plot every month the mouse has, so the title says so
        super().__init__(title + ", All Time", parent)
        self.unit = unit
        self.empty = empty
        self.caption = caption
        self.series = {}

    def setSeries(self, series):
        self.series = {label: points for label, points in series.items() if points}
        self.update()

    def months(self):
        return sorted({month for points in self.series.values() for month, value in points})

    def headline(self):
        # the newest month across every series, and the first series in name order when two share it
        newest = None
        for label in sorted(self.series):
            month, value = self.series[label][-1]
            if (newest is None or month > newest[0]):
                newest = (month, value)
        return newest

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
        for index, (label, points) in enumerate(sorted(self.series.items())):
            color = colors[index % len(colors)]
            positions = [QPointF(area.x() + area.width() * (months.index(month) / max(1, len(months) - 1) if len(months) > 1 else 0.5), area.bottom() - (value - low) / (high - low) * area.height()) for month, value in points]
            self.paintLine(painter, area, positions, color, fade=len(self.series) == 1)
            legend.append((label, color))
        latestMonth, latest = self.headline()
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
            name = mice.MONTH_NAMES[time.strptime(months[index], "%Y-%m").tm_mon - 1]
            painter.drawText(QRectF(places[index] - MONTH_LABEL_WIDTH / 2, area.bottom() + 6, MONTH_LABEL_WIDTH, 16), int(Qt.AlignmentFlag.AlignCenter), name)
