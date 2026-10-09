import ctypes
import os
import time

from PyQt6 import sip
from PyQt6.QtCore import QElapsedTimer, QPoint, QPointF, QRectF, QSizeF, Qt, QTimer, QVariantAnimation, QEasingCurve, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusMessage
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget

import charts
import dbusCalls
import glass
import health
import hidpp
import history
import icon
import mice
import panelColor
import serviceClient
import serviceState

PANE_WIDTH = 760
BASE_HEIGHT = 560
MAX_HEIGHT = 640
SIDEBAR_WIDTH = 232
MARGIN = 24
ICON_SIZE = 120
HEALTH_WIDTH = 356
HEALTH_MIN_HEIGHT = 112
TOP_ROW = 120
SEGMENT_HEIGHT = 32
SEGMENT_WIDTHS = (76, 134, 92)
FORM_HEIGHT = 72
ROW_HEIGHT = 46
CHART_HEIGHT = 238
CHART_MIN_HEIGHT = 160
# the layout's fixed parts, top margin to bottom margin, around the top row and the chart
FIXED_HEIGHT = MARGIN + 20 + SEGMENT_HEIGHT + 14 + 16 + FORM_HEIGHT + MARGIN
FETCH_GAP_SECONDS = 30
# the service writes a UPower reading only once its wake set wait is over, so a fetch after a change waits that long and a bit for the GetAll each side makes
UPOWER_SETTLE_SECONDS = history.WAKE_SET_SECONDS + 3
STALE_SLOT_SECONDS = 2 * 60 * 60
RETRY_MS = 30 * 1000
# KWin answered within 20 ms where it was measured, so the first check comes soon and a slower machine gets one more
BLUR_CHECK_MS = (25, 100)
CHART_FADE_MS = 180
PANE_FADE_MS = 180
THUMB_SLIDE_MS = 220
GLOW_FADE_MS = 400
FROST_EVERY_MS = 50
SLEEP_CHOICES = ((0, "Off"), (30, "Every 30 seconds"), (60, "Every minute"), (300, "Every 5 minutes"))
# the button shows the choice short, so the label beside it fits, while its menu names each one in full
SLEEP_SHORT = {0: "Off", 30: "30 s", 60: "1 min", 300: "5 min"}
SLEEP_LABEL = "Check whether mice are asleep"
CHOICE_ROW_HEIGHT = 26
CHOICE_PADDING = 5
CHOICE_TEXT_X = 26
CHOICE_RADIUS = 10
CHOICE_MARGIN = 8
SEGMENTS = ("Level", "Hours per Charge", "Charging")
LOADING_TEXT = "Loading"
NO_SERVICE_TEXT = "Can't reach the battery service."
NO_SERIAL_TEXT = "This mouse doesn't give a serial number, so its history can't be kept."
UNAVAILABLE_TEXT = "Its history isn't available right now."
RESTART_TEXT = "Restart Mouse Battery to see its history."
RANGE_RESTART_TEXT = "Restart Mouse Battery to see this range."
RANGE_ERROR_TEXT = "Couldn't load this range."
RANGE_NAMES = ("day", "week", "month", "halfYear", "year", "all")
RANGE_LABELS = ("D", "W", "M", "6M", "Y", "All")
RANGE_WIDTHS = (30, 30, 30, 34, 30, 36)
RANGE_HEIGHT = 22
# an older service answers these the same way every time, so asking again changes nothing
RESTART_LEVEL_ERRORS = (serviceState.UNKNOWN_METHOD, serviceState.ACCESS_DENIED, serviceState.INVALID_ARGS)
FINAL_ERRORS = RESTART_LEVEL_ERRORS + (serviceClient.UNDECODABLE,)
# a refused range waits out the refill of its budget before it's asked for again
LEVEL_RETRY_MS = serviceState.LEVELS_REFILL_SECONDS * 1000
NOT_REFRESHED_TEXT = "Couldn't refresh the history."


def font(size, weight=QFont.Weight.Normal):
    chosen = QFont()
    chosen.setPixelSize(size)
    chosen.setWeight(weight)
    return chosen


def capitalized(text):
    return text[:1].upper() + text[1:]


BLUR_SYMBOL = "_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion"
AVAILABLE_SYMBOL = "_ZN14KWindowEffects17isEffectAvailableENS_6EffectE"
# KWindowEffects::BlurBehind in kwindoweffects.h
BLUR_BEHIND = 7


def windowSystemCall(symbol):
    # KWin blurs what's behind a window that asks through KWindowSystem, which has no Python bindings, so it's called through ctypes, and any failure leaves the painted backdrop alone
    if ("KDE" not in panelColor.desktopNames(os.environ)):
        return None
    try:
        return getattr(ctypes.CDLL("libKF6WindowSystem.so.6"), symbol)
    except (OSError, AttributeError):
        return None


def desktopBlurAvailable():
    # with KWin's blur effect turned off the window would show the desktop sharp, so it keeps its opaque backdrop
    available = windowSystemCall(AVAILABLE_SYMBOL)
    if (available is None):
        return False
    available.argtypes = [ctypes.c_int]
    available.restype = ctypes.c_bool
    return bool(available(BLUR_BEHIND))


def enableDesktopBlur(widget):
    enable = windowSystemCall(BLUR_SYMBOL)
    if (enable is None):
        return False
    handle = widget.windowHandle()
    if (handle is None):
        return False
    enable.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]
    enable.restype = None
    region = QRegion()
    enable(sip.unwrapinstance(handle), True, sip.unwrapinstance(region))
    return True


def label(parent, size, weight=QFont.Weight.Normal, wrap=False):
    widget = QLabel(parent)
    widget.setFont(font(size, weight))
    widget.setWordWrap(wrap)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    return widget


def statusWords(mouse):
    # the menu's own words for the state, without the level, since the level sits right above in large type
    reading = mouse.reading
    if (reading is None):
        return "No reading yet"
    if (mouse.stale):
        return "Last read " + mice.readingTime(mouse.lastRead, time.time())
    if (mouse.asleep):
        return "Asleep, read " + mice.readingTime(mouse.lastRead, time.time())
    words = {hidpp.CHARGING_CHARGING: "Charging", hidpp.CHARGING_SLOW: "Charging slowly", hidpp.CHARGING_FULL: "Full", hidpp.CHARGING_ERROR: "Charging error", hidpp.CHARGING_PENDING: "Pending charge", hidpp.CHARGING_UNKNOWN: "Unknown state"}
    return words.get(reading.charging, "In use")


class Reply:
    def __init__(self, summary, revision, askedAt):
        self.summary = summary
        self.revision = revision
        self.askedAt = askedAt
        recent = summary.get("recent") or []
        self.newestSlot = recent[-1][0] if recent else time.time()


class HealthSymbol(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.symbol = None
        self.theme = glass.DARK
        self.setFixedSize(18, 18)

    def setSymbol(self, symbol, theme):
        self.symbol = symbol
        self.theme = theme
        self.update()

    def paintEvent(self, event):
        if (self.symbol is None):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = {health.SYMBOL_CHECK: self.theme.green, health.SYMBOL_WARNING: self.theme.orange, health.SYMBOL_CLOCK: glass.GRAY}[self.symbol]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(1, 1, 16, 16))
        pen = QPen(QColor("#FFFFFF"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if (self.symbol == health.SYMBOL_CHECK):
            painter.drawLine(QPointF(5.5, 9), QPointF(8.2, 11.8))
            painter.drawLine(QPointF(8.2, 11.8), QPointF(13, 6.2))
        elif (self.symbol == health.SYMBOL_WARNING):
            painter.drawLine(QPointF(9, 5), QPointF(9, 10))
            painter.drawPoint(QPointF(9, 13))
        else:
            painter.drawLine(QPointF(9, 9), QPointF(9, 5.5))
            painter.drawLine(QPointF(9, 9), QPointF(11.8, 10.6))


class MouseIcon(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.image = None
        self.setFixedSize(ICON_SIZE, ICON_SIZE)

    def setState(self, state, theme):
        if (state is None):
            self.image = None
        else:
            ratio = self.devicePixelRatioF()
            # drawn at the screen's pixel ratio, so it stays sharp on a HiDPI screen
            self.image = icon.renderImage(state, round(ICON_SIZE * ratio), theme.iconBase, number=False)
            self.image.setDevicePixelRatio(ratio)
        self.update()

    def paintEvent(self, event):
        if (self.image is not None):
            QPainter(self).drawImage(QRectF(0, 0, ICON_SIZE, ICON_SIZE), self.image)


class SegmentedControl(QWidget):
    chosen = pyqtSignal(int)

    def __init__(self, window, names=SEGMENTS, widths=SEGMENT_WIDTHS, height=SEGMENT_HEIGHT, fontSize=12, track=False, index=0):
        super().__init__(window)
        self.owner = window
        self.names = names
        self.widths = widths
        self.fontSize = fontSize
        # a control inside a panel draws its own track, while the main one sits on a panel the window paints
        self.track = track
        self.inset = 4.0 if height >= 28 else 2.0
        self.index = index
        self.thumbX = self.segmentX(index)
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self.onSlide)
        self.setFixedSize(int(sum(widths) + 2 * self.inset), height)

    def segmentX(self, index):
        return self.inset + sum(self.widths[:index])

    def choose(self, index, animate=True):
        if (index == self.index):
            return
        self.index = index
        duration = int(THUMB_SLIDE_MS * self.owner.motionFactor)
        self.animation.stop()
        if (animate and duration > 0):
            self.animation.setStartValue(self.thumbX)
            self.animation.setEndValue(self.segmentX(index))
            self.animation.setDuration(duration)
            self.animation.start()
        else:
            self.thumbX = self.segmentX(index)
            self.update()
        self.chosen.emit(index)

    def onSlide(self, value):
        self.thumbX = value
        self.update()

    def mousePressEvent(self, event):
        x = event.position().x()
        for index in range(len(self.names)):
            if (self.segmentX(index) <= x < self.segmentX(index + 1)):
                self.choose(index)
                return

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        frost = self.owner.frost()
        if (self.track):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glass.rgba(theme.grid))
            painter.drawRoundedRect(QRectF(self.rect()), self.height() / 2, self.height() / 2)
        if (frost is not None):
            thumb = QRectF(self.thumbX, self.inset, self.widths[self.index], self.height() - 2 * self.inset)
            glass.paintPanel(painter, thumb, thumb.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
        for index, name in enumerate(self.names):
            painter.setFont(font(self.fontSize, QFont.Weight.DemiBold if index == self.index else QFont.Weight.Normal))
            painter.setPen(QColor(theme.text))
            painter.drawText(QRectF(self.segmentX(index), 0, self.widths[index], self.height()), int(Qt.AlignmentFlag.AlignCenter), name)


class ChoiceMenu(QWidget):
    # drawn inside the window instead of as a QMenu, so its glass comes from the same frost as the panels around it
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.choices = ()
        self.marked = 0
        self.hover = 0
        self.onPick = None
        self.panel = QRectF()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(SLEEP_LABEL)
        self.hide()

    def open(self, choices, value, anchor, onPick):
        self.choices = choices
        self.marked = next((index for index, (choice, name) in enumerate(choices) if choice == value), 0)
        self.hover = self.marked
        self.onPick = onPick
        self.setGeometry(self.owner.rect())
        width = max(anchor.width(), CHOICE_TEXT_X + max(QFontMetricsF(font(13)).horizontalAdvance(name) for choice, name in choices) + CHOICE_PADDING * 3)
        height = len(choices) * CHOICE_ROW_HEIGHT + 2 * CHOICE_PADDING
        # the marked row opens over the button, the way a macOS pop up button does, kept inside the window so the frost is under all of it
        top = anchor.center().y() - CHOICE_PADDING - self.marked * CHOICE_ROW_HEIGHT - CHOICE_ROW_HEIGHT / 2
        top = min(max(top, CHOICE_MARGIN), self.height() - CHOICE_MARGIN - height)
        left = min(max(anchor.right() - width, CHOICE_MARGIN), self.width() - CHOICE_MARGIN - width)
        self.panel = QRectF(left, top, width, height)
        self.show()
        self.raise_()
        self.setFocus()
        self.update()

    def rowRect(self, index):
        return QRectF(self.panel.x() + CHOICE_PADDING, self.panel.y() + CHOICE_PADDING + index * CHOICE_ROW_HEIGHT, self.panel.width() - 2 * CHOICE_PADDING, CHOICE_ROW_HEIGHT)

    def rowAt(self, point):
        return next((index for index in range(len(self.choices)) if self.rowRect(index).contains(point)), None)

    def choose(self, index):
        self.hide()
        self.onPick(self.choices[index][0])

    def mouseMoveEvent(self, event):
        index = self.rowAt(event.position())
        if (index is not None and index != self.hover):
            self.hover = index
            self.update()

    def mousePressEvent(self, event):
        if (not self.panel.contains(event.position())):
            self.hide()

    def mouseReleaseEvent(self, event):
        index = self.rowAt(event.position())
        if (index is not None and self.isVisible()):
            self.choose(index)

    def keyPressEvent(self, event):
        key = event.key()
        if (key == Qt.Key.Key_Escape):
            self.hide()
        elif (key in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.hover = max(0, min(len(self.choices) - 1, self.hover + (1 if key == Qt.Key.Key_Down else -1)))
            self.update()
        elif (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)):
            self.choose(self.hover)

    def focusOutEvent(self, event):
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        frost = self.owner.frost()
        glass.paintShadow(painter, self.panel, CHOICE_RADIUS, theme)
        if (frost is not None):
            glass.paintPanel(painter, self.panel, CHOICE_RADIUS, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, QPoint(0, 0))))
        for index, (choice, name) in enumerate(self.choices):
            rect = self.rowRect(index)
            textColor = QColor(theme.text)
            if (index == self.hover):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(glass.BLUE))
                painter.drawRoundedRect(rect, 6, 6)
                textColor = QColor("#FFFFFF")
            if (index == self.marked):
                # one check mark, since only one choice can be on
                painter.setPen(QPen(textColor, 1.8, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
                middle = rect.center().y()
                painter.drawPolyline([QPointF(rect.x() + 9, middle), QPointF(rect.x() + 12.5, middle + 3.5), QPointF(rect.x() + 19, middle - 4)])
            painter.setFont(font(13))
            painter.setPen(textColor)
            painter.drawText(QRectF(rect.x() + CHOICE_TEXT_X, rect.y(), rect.width() - CHOICE_TEXT_X, rect.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)


class PopupButton(QWidget):
    picked = pyqtSignal(int)

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.seconds = 0
        self.setFixedSize(76, 24)

    def setSeconds(self, seconds):
        self.seconds = seconds
        # painted instead of a QComboBox, so a screen reader gets its name and value from here
        self.setAccessibleName(SLEEP_LABEL + ", " + self.text())
        self.update()

    def text(self):
        return SLEEP_SHORT.get(self.seconds, "Off")

    def mousePressEvent(self, event):
        anchor = QRectF(QPointF(self.mapTo(self.owner, QPoint(0, 0))), QSizeF(self.size()))
        self.owner.choiceMenu.open(SLEEP_CHOICES, self.seconds, anchor, self.pick)

    def pick(self, seconds):
        self.setSeconds(seconds)
        self.picked.emit(seconds)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        frost = self.owner.frost()
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if (frost is not None):
            glass.paintPanel(painter, rect, rect.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
        painter.setFont(font(12))
        painter.setPen(QColor(theme.text))
        painter.drawText(QRectF(12, 0, self.width() - 34, self.height()), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
        painter.setPen(QPen(QColor(theme.secondary), 1.4, cap=Qt.PenCapStyle.RoundCap))
        x = self.width() - 14
        middle = self.height() / 2
        painter.drawLine(QPointF(x - 3, middle - 2), QPointF(x, middle - 5))
        painter.drawLine(QPointF(x, middle - 5), QPointF(x + 3, middle - 2))
        painter.drawLine(QPointF(x - 3, middle + 2), QPointF(x, middle + 5))
        painter.drawLine(QPointF(x, middle + 5), QPointF(x + 3, middle + 2))


class Sidebar(QWidget):
    picked = pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.rows = []
        self.selected = None

    def setRows(self, rows, selected):
        # each row is (key, name, second line, capsule fill from 0 to 1 or None, color name of the second line)
        self.rows = rows
        self.selected = selected
        self.update()

    def rowRect(self, index):
        return QRectF(10, 40 + index * (ROW_HEIGHT + 4), self.width() - 20, ROW_HEIGHT)

    def mousePressEvent(self, event):
        for index, row in enumerate(self.rows):
            if (self.rowRect(index).contains(event.position())):
                self.picked.emit(row[0])
                return

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        painter.setFont(font(11, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.secondary))
        painter.drawText(QRectF(20, 12, 200, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "Mice")
        frost = self.owner.frost()
        for index, (key, name, second, fill, secondColor) in enumerate(self.rows):
            rect = self.rowRect(index)
            if (key == self.selected and frost is not None):
                glass.paintPanel(painter, rect, rect.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
            self.paintCapsule(painter, QRectF(rect.x() + 12, rect.y() + 10, 22, 11), fill, theme, secondColor)
            painter.setFont(font(13, QFont.Weight.Medium))
            painter.setPen(QColor(theme.text))
            painter.drawText(QRectF(rect.x() + 44, rect.y() + 4, rect.width() - 52, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(rect.width() - 52)))
            painter.setFont(font(11))
            painter.setPen(QColor(secondColor))
            painter.drawText(QRectF(rect.x() + 44, rect.y() + 23, rect.width() - 52, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), second)

    def paintCapsule(self, painter, rect, fill, theme, color):
        painter.setPen(QPen(QColor(theme.secondary), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 3, 3)
        painter.fillRect(QRectF(rect.right() + 1, rect.y() + 3.5, 1.6, 4), QColor(theme.secondary))
        if (fill is not None and fill > 0):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color if color != theme.secondary else theme.text))
            painter.drawRoundedRect(QRectF(rect.x() + 2, rect.y() + 2, (rect.width() - 4) * min(1.0, fill), rect.height() - 4), 1.5, 1.5)


class HistoryWindow(QWidget):
    sleepCheckChanged = pyqtSignal(int)
    levelRangeChanged = pyqtSignal(str)

    def __init__(self, mouseList, serviceWatcher, upowerWatcher, configHome, sessionBus, lineFor, iconStateFor, sleepCheckSeconds=0, levelRange="week"):
        super().__init__()
        self.mouseList = mouseList
        self.serviceWatcher = serviceWatcher
        self.configHome = configHome
        self.sessionBus = sessionBus
        # the tray owns the menu line and the icon state, and the window reads them through these so the two always agree
        self.lineFor = lineFor
        self.iconStateFor = iconStateFor
        self.setWindowTitle("Mouse Battery")
        self.portalScheme = 0
        self.reducedMotion = 0
        self.motionFactor = 1.0
        self.theme = glass.themeFor(glass.isDark(0, QApplication.instance().palette()))
        self.glow = QColor(glass.GRAY)
        self.backdrop = None
        self.frostImage = None
        self.frostKey = None
        self.frostClock = QElapsedTimer()
        self.replies = {}
        self.errors = {}
        self.notRefreshed = set()
        self.pending = set()
        self.lastFetch = {}
        self.upowerChangedAt = {}
        self.levelRange = levelRange if levelRange in RANGE_NAMES else "week"
        # each mouse's slots per range, dropped with its history reply, since a new reply means new readings
        self.levels = {}
        # the ranges asked for since each mouse's last history reply, while older slots stay drawn until their new answer
        self.freshLevels = set()
        self.levelErrors = {}
        self.pendingLevels = set()
        self.selected = None
        self.wanted = None
        self.health = None
        self.panels = []
        self.makeParts(sleepCheckSeconds)
        self.fetchTimer = mice.singleShotTimer(self, 1000, self.fetchSelected)
        self.retryTimer = mice.singleShotTimer(self, RETRY_MS, self.fetchSelected)
        self.levelRetryTimer = mice.singleShotTimer(self, LEVEL_RETRY_MS, self.retryLevels)
        self.blurTimer = mice.singleShotTimer(self, BLUR_CHECK_MS[0], self.checkBlur)
        self.blurChecks = 0
        self.ageTimer = mice.singleShotTimer(self, 1000, self.fetchSelected)
        self.glowAnimation = QVariantAnimation(self)
        self.glowAnimation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.glowAnimation.valueChanged.connect(self.onGlowStep)
        self.glowAnimation.finished.connect(lambda: self.remakeFrost(force=True))
        mouseList.changed.connect(self.refresh)
        serviceWatcher.serviceRestarted.connect(self.forgetReplies)
        if (upowerWatcher is not None):
            upowerWatcher.mouseChanged.connect(self.onUPowerMouse)
        QApplication.instance().paletteChanged.connect(self.onPaletteChanged)
        self.watchingPortal = False
        self.blurred = False

    def makeParts(self, sleepCheckSeconds):
        self.sidebar = Sidebar(self)
        self.sidebar.picked.connect(self.pick)
        self.pane = QWidget(self)
        self.pane.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.paneFade = QGraphicsOpacityEffect(self.pane)
        self.paneFade.setOpacity(1.0)
        # an opacity effect draws its widget from a cached image, and with the chart's own fade inside it a changed chart never showed, so each fade is only on while it runs
        self.paneFade.setEnabled(False)
        self.pane.setGraphicsEffect(self.paneFade)
        self.paneAnimation = QVariantAnimation(self)
        self.paneAnimation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.paneAnimation.valueChanged.connect(lambda value: self.paneFade.setOpacity(value))
        self.paneAnimation.finished.connect(lambda: self.paneFade.setEnabled(False))
        self.mouseIcon = MouseIcon(self.pane)
        self.nameLabel = label(self.pane, 12, QFont.Weight.Medium)
        self.levelLabel = label(self.pane, 40, QFont.Weight.Bold)
        self.statusLabel = label(self.pane, 13, wrap=True)
        self.healthTitle = label(self.pane, 12, QFont.Weight.Medium)
        self.healthTitle.setText("Battery Health")
        self.symbol = HealthSymbol(self.pane)
        self.lineLabels = [label(self.pane, 13 if index == 0 else 12, QFont.Weight.DemiBold if index == 0 else QFont.Weight.Normal, wrap=True) for index in range(3)]
        self.refreshLabel = label(self.pane, 11, wrap=True)
        self.segments = SegmentedControl(self)
        self.segments.setParent(self.pane)
        self.segments.chosen.connect(self.showChart)
        self.levelChart = charts.LevelChart(self.pane)
        self.hoursChart = charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, charts.HOURS_CAPTION, self.pane)
        self.chargingChart = charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, None, self.pane)
        self.charts = [self.levelChart, self.hoursChart, self.chargingChart]
        # made after the charts, so it sits above the level chart's corner
        self.rangeControl = SegmentedControl(self, RANGE_LABELS, RANGE_WIDTHS, RANGE_HEIGHT, 11, track=True, index=RANGE_NAMES.index(self.levelRange))
        self.rangeControl.setParent(self.pane)
        self.rangeControl.chosen.connect(lambda index: self.pickRange(RANGE_NAMES[index]))
        self.nameRangeControl()
        self.chartFade = QGraphicsOpacityEffect(self.levelChart)
        self.chartAnimation = QVariantAnimation(self)
        self.chartAnimation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.chartAnimation.valueChanged.connect(lambda value: self.chartFade.setOpacity(value))
        self.chartAnimation.finished.connect(lambda: self.chartFade.setEnabled(False))
        self.formLabels = [label(self.pane, 13) for index in range(4)]
        self.formValues = [label(self.pane, 13) for index in range(3)]
        for widget, text in zip(self.formLabels, ("Charge cycles", "Last charged", "Charging speed", "Check whether mice are asleep")):
            widget.setText(text)
        for widget in self.formValues:
            widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.popup = PopupButton(self)
        self.popup.setParent(self.pane)
        self.popup.setSeconds(sleepCheckSeconds)
        self.popup.picked.connect(self.sleepCheckChanged.emit)
        self.choiceMenu = ChoiceMenu(self)
        self.message = label(self.pane, 13, wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paneMessage = label(self.pane, 13, wrap=True)
        self.paneMessage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for chart in self.charts[1:]:
            chart.hide()

    def frost(self):
        return self.frostImage

    def open(self):
        self.motionFactor = glass.animationFactor(self.configHome, False)
        self.readPortal()
        # shown before the refresh, since the window only asks for a history while it's visible
        if (not self.isVisible()):
            # a window opened from the tray starts on the tray's mouse
            self.selected = None
            self.wanted = None
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.show()
            # the blur is asked for once the window has a handle, and a window without it keeps its painted backdrop opaque
            if (enableDesktopBlur(self)):
                self.blurred = desktopBlurAvailable()
                if (not self.blurred):
                    # KWin's answer comes after the event loop has run once, measured at under 20 ms
                    self.blurChecks = 0
                    self.blurTimer.start(BLUR_CHECK_MS[0])
            else:
                self.blurred = False
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                self.update()
        else:
            self.show()
        self.refresh()
        self.raise_()
        self.activateWindow()

    def checkBlur(self):
        self.blurred = desktopBlurAvailable()
        self.blurChecks += 1
        if (not self.blurred and self.blurChecks < len(BLUR_CHECK_MS)):
            self.blurTimer.start(BLUR_CHECK_MS[self.blurChecks])
        self.update()

    def readPortal(self):
        if (self.sessionBus is None):
            return
        if (not self.watchingPortal):
            self.watchingPortal = True
            self.sessionBus.connect(panelColor.PORTAL_SERVICE, panelColor.PORTAL_PATH, panelColor.SETTINGS_INTERFACE, "SettingChanged", self.onPortalSetting)
        panelColor.askPortalSetting(self.sessionBus, self, "color-scheme", lambda value, error: self.setPortal("color-scheme", value if error is None else 0))
        # a portal older than the key answers with an error, and then motion follows AnimationDurationFactor alone
        panelColor.askPortalSetting(self.sessionBus, self, "reduced-motion", lambda value, error: self.setPortal("reduced-motion", value if error is None else 0))

    @pyqtSlot(QDBusMessage)
    def onPortalSetting(self, message):
        arguments = message.arguments()
        if (len(arguments) >= 3 and arguments[0] == panelColor.APPEARANCE and arguments[1] in ("color-scheme", "reduced-motion")):
            value = dbusCalls.plain(arguments[2])
            self.setPortal(arguments[1], value if isinstance(value, int) else 0)

    def setPortal(self, key, value):
        if (key == "color-scheme"):
            self.portalScheme = value
            self.applyTheme()
        else:
            self.reducedMotion = value
            self.motionFactor = glass.animationFactor(self.configHome, value == 1)

    def onPaletteChanged(self, *args):
        self.applyTheme()

    def applyTheme(self):
        theme = glass.themeFor(glass.isDark(self.portalScheme, QApplication.instance().palette()))
        if (theme == self.theme and self.backdrop is not None):
            return
        self.theme = theme
        self.backdrop = None
        self.refresh()

    def forgetReplies(self):
        # revisions start over with the service, so every kept reply could look current while it isn't
        self.replies.clear()
        self.errors.clear()
        self.notRefreshed.clear()
        self.forgetLevels()
        self.refresh()

    def onUPowerMouse(self, upowerMouse):
        # kept by key, so an answer to a fetch still on its way when the change came is old too
        self.upowerChangedAt[upowerMouse.key] = time.monotonic()
        if (upowerMouse.key == self.selected):
            self.refresh()

    def pick(self, key):
        if (key == self.selected):
            return
        self.selected = key
        duration = int(PANE_FADE_MS * self.motionFactor)
        self.paneAnimation.stop()
        if (duration > 0 and self.isVisible()):
            self.paneFade.setOpacity(0.0)
            self.paneFade.setEnabled(True)
            self.paneAnimation.setStartValue(0.0)
            self.paneAnimation.setEndValue(1.0)
            self.paneAnimation.setDuration(duration)
            self.paneAnimation.start()
        else:
            self.paneFade.setEnabled(False)
        self.refresh()

    def nameRangeControl(self):
        self.rangeControl.setAccessibleName("Chart range, " + charts.RANGE_TITLES[self.levelRange].split(", ", 1)[1])

    def pickRange(self, rangeName):
        if (rangeName == self.levelRange):
            return
        self.levelRange = rangeName
        self.rangeControl.choose(RANGE_NAMES.index(rangeName))
        self.nameRangeControl()
        self.levelRangeChanged.emit(rangeName)
        self.refresh()

    def fetchLevels(self, key, rangeName):
        if ((key, rangeName) in self.pendingLevels or not self.isVisible()):
            return
        self.pendingLevels.add((key, rangeName))
        self.serviceWatcher.getLevels(key, rangeName, lambda levels, error: self.onLevels(key, rangeName, levels, error))

    def onLevels(self, key, rangeName, levels, error):
        self.pendingLevels.discard((key, rangeName))
        if (levels is not None):
            self.levels.setdefault(key, {})[rangeName] = levels["slots"]
            self.freshLevels.add((key, rangeName))
            self.levelErrors.pop((key, rangeName), None)
        else:
            self.levelErrors[(key, rangeName)] = error
            if (error not in FINAL_ERRORS):
                self.levelRetryTimer.start(LEVEL_RETRY_MS if error == serviceState.READ_ALL_LIMITED else RETRY_MS)
        self.refresh()

    def retryLevels(self):
        for item, error in list(self.levelErrors.items()):
            if (error not in FINAL_ERRORS):
                del self.levelErrors[item]
        self.refresh()

    def forgetLevels(self, key=None):
        if (key is None):
            self.levels.clear()
            self.freshLevels.clear()
            self.levelErrors.clear()
            return
        # the slots stay drawn until the new answer, so a new reading never blanks the chart
        self.freshLevels = {item for item in self.freshLevels if item[0] != key}
        for item in [item for item in self.levelErrors if item[0] == key]:
            del self.levelErrors[item]

    def showChart(self, index):
        self.rangeControl.setVisible(index == 0)
        chart = self.charts[index]
        for other in self.charts:
            if (other is not chart):
                other.hide()
        self.chartFade = QGraphicsOpacityEffect(chart)
        chart.setGraphicsEffect(self.chartFade)
        chart.show()
        duration = int(CHART_FADE_MS * self.motionFactor)
        self.chartAnimation.stop()
        if (duration > 0):
            self.chartFade.setOpacity(0.0)
            self.chartAnimation.setStartValue(0.0)
            self.chartAnimation.setEndValue(1.0)
            self.chartAnimation.setDuration(duration)
            self.chartAnimation.start()
        else:
            self.chartFade.setEnabled(False)

    def closeEvent(self, event):
        # the window is kept for the next click, and so are its replies, its ranges, and any answer still on its way, so the next open shows them and isOld decides what to fetch, while an error gets one more try then
        self.errors.clear()
        self.levelErrors.clear()
        self.choiceMenu.hide()
        for timer in (self.fetchTimer, self.retryTimer, self.ageTimer, self.blurTimer, self.levelRetryTimer):
            timer.stop()
        event.accept()

    def chooseSelected(self, keys):
        if (self.selected in keys):
            return
        if (self.selected is not None and self.wanted is not None and self.wanted < len(keys)):
            # the mouse whose row took the gone one's place, or the last when it was at the bottom
            self.selected = keys[self.wanted]
            return
        if (self.selected is not None and keys):
            self.selected = keys[-1]
            return
        shown = self.mouseList.shownMouse()
        self.selected = shown.key if shown is not None else (keys[0] if keys else None)

    def selectedMouse(self):
        return next((mouse for mouse in self.mouseList.mice if mouse.key == self.selected), None)

    def refresh(self):
        keys = [mouse.key for mouse in self.mouseList.mice]
        self.chooseSelected(keys)
        if (self.selected in keys):
            self.wanted = keys.index(self.selected)
        mouse = self.selectedMouse()
        if (self.isVisible() and mouse is not None):
            self.considerFetch(mouse)
        self.fillSidebar(keys)
        self.fillPane(mouse)
        self.layoutParts()
        self.update()

    def considerFetch(self, mouse):
        if (mouse.pathKey or self.mouseList.serviceLost() or mouse.key in self.pending):
            return
        # the same answer would come back, so only a service restart or the next open asks again
        if (self.errors.get(mouse.key) in FINAL_ERRORS):
            return
        reply = self.replies.get(mouse.key)
        if (reply is not None and not self.isOld(mouse, reply)):
            self.scheduleAge(reply)
            return
        self.requestFetch(mouse.key)

    def isOld(self, mouse, reply):
        # a UPower mouse has no revision, so a reply asked for before the service saved the change the tray's own watcher saw is old instead
        changedAt = self.upowerChangedAt.get(mouse.key)
        upowerOld = changedAt is not None and reply.askedAt < changedAt + UPOWER_SETTLE_SECONDS
        return mouse.historyRevision != reply.revision or upowerOld or reply.newestSlot + STALE_SLOT_SECONDS <= time.time()

    def scheduleAge(self, reply):
        # the 7 day chart and the 7 day checks keep moving, so a reply goes old two hours after its newest slot
        wait = reply.newestSlot + STALE_SLOT_SECONDS - time.time()
        # a clock that jumped would otherwise make a wait no timer can hold
        self.ageTimer.start(max(1000, int(min(wait, STALE_SLOT_SECONDS) * 1000)))

    def requestFetch(self, key):
        now = time.monotonic()
        due = now
        last = self.lastFetch.get(key)
        # at most once every 30 s, so an open window stays inside the GetHistory budget
        if (last is not None):
            due = max(due, last + FETCH_GAP_SECONDS)
        changedAt = self.upowerChangedAt.get(key)
        if (changedAt is not None):
            due = max(due, changedAt + UPOWER_SETTLE_SECONDS)
        if (due > now):
            wait = max(1, int((due - now) * 1000))
            # one timer serves every mouse, so the shown mouse's shorter wait replaces another's longer one
            if (not self.fetchTimer.isActive() or self.fetchTimer.remainingTime() > wait):
                self.fetchTimer.start(wait)
            return
        mouse = next((each for each in self.mouseList.mice if each.key == key), None)
        if (mouse is None):
            return
        self.lastFetch[key] = now
        self.pending.add(key)
        revision = mouse.historyRevision
        self.serviceWatcher.getHistory(key, lambda summary, error: self.onHistory(key, revision, now, summary, error))

    def fetchSelected(self):
        mouse = self.selectedMouse()
        if (mouse is not None and self.isVisible()):
            self.considerFetch(mouse)

    def onHistory(self, key, revision, askedAt, summary, error):
        self.pending.discard(key)
        if (summary is not None):
            self.replies[key] = Reply(summary, revision, askedAt)
            self.forgetLevels(key)
            self.errors.pop(key, None)
            self.notRefreshed.discard(key)
        elif (key in self.replies):
            self.notRefreshed.add(key)
            self.errors[key] = error
        else:
            self.errors[key] = error
            if (error == serviceState.HISTORY_UNAVAILABLE and self.isVisible()):
                self.retryTimer.start()
        self.refresh()

    def fillSidebar(self, keys):
        show = len(keys) >= 2
        self.sidebar.setVisible(show)
        if (not show):
            return
        names = self.mouseList.displayNames()
        rows = []
        for mouse in self.mouseList.mice:
            second = self.lineFor(names[mouse.key], mouse)[len(names[mouse.key]) + 2:]
            rows.append((mouse.key, names[mouse.key], second, self.capsuleFill(mouse), self.secondColor(mouse)))
        self.sidebar.setRows(rows, self.selected)

    def capsuleFill(self, mouse):
        reading = mouse.reading
        if (reading is None):
            return None
        if (reading.percent is not None):
            return reading.percent / 100
        if (reading.level is not None and reading.level in charts.LEVEL_HEIGHTS):
            return charts.LEVEL_HEIGHTS[reading.level] / 100
        return None

    def secondColor(self, mouse):
        reading = mouse.reading
        if (reading is None or mouse.asleep):
            return self.theme.secondary
        if (reading.charging in (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW, hidpp.CHARGING_FULL)):
            return self.theme.green
        if (reading.percent is not None and reading.percent <= icon.RED_PERCENT):
            return self.theme.red
        return self.theme.secondary

    def historyMessage(self, mouse):
        if (mouse.pathKey):
            return NO_SERIAL_TEXT
        if (self.mouseList.serviceLost()):
            return NO_SERVICE_TEXT
        if (mouse.key in self.replies):
            return None
        error = self.errors.get(mouse.key)
        if (error == serviceState.HISTORY_UNAVAILABLE):
            return UNAVAILABLE_TEXT
        if (error in (serviceState.UNKNOWN_METHOD, serviceState.ACCESS_DENIED)):
            return RESTART_TEXT
        if (error is not None):
            return UNAVAILABLE_TEXT
        return LOADING_TEXT

    def fillPane(self, mouse):
        theme = self.theme
        for widget in (self.nameLabel, self.healthTitle, self.refreshLabel):
            widget.setStyleSheet("color: " + theme.secondary)
        for widget in [self.levelLabel, self.statusLabel, self.message, self.paneMessage] + self.formLabels:
            widget.setStyleSheet("color: " + theme.text)
        for widget in self.formValues:
            widget.setStyleSheet("color: " + theme.secondary)
        for chart in self.charts:
            chart.setTheme(theme)
        if (mouse is None):
            self.health = None
            self.paneMessage.setText(mice.STATUS_TEXT[self.mouseList.status()])
            self.showParts(summary=False, history=False, paneMessage=True)
            self.setGlow(None)
            return
        name = self.mouseList.displayNames()[mouse.key]
        reply = self.replies.get(mouse.key)
        self.health = health.assess(reply.summary, mouse.reading) if reply is not None else None
        self.mouseIcon.setState(self.iconStateFor(mouse), theme)
        self.nameLabel.setText(name.upper())
        self.levelLabel.setText(capitalized(hidpp.levelText(mouse.reading)) if mouse.reading is not None else "")
        self.statusLabel.setText(statusWords(mouse) + (health.statusSuffix(self.health) if self.health is not None else ""))
        messageText = self.historyMessage(mouse)
        if (messageText is not None):
            self.message.setText(messageText)
            self.showParts(summary=True, history=False, paneMessage=False)
            self.fillForm(None)
        else:
            self.showParts(summary=True, history=True, paneMessage=False)
            self.fillHealth(theme)
            self.fillCharts(reply.summary, mouse.reading, mouse.key)
            self.fillForm(self.health)
            self.refreshLabel.setText(NOT_REFRESHED_TEXT if mouse.key in self.notRefreshed else "")
        warning = self.health is not None and self.health.symbol == health.SYMBOL_WARNING
        self.setGlow(glass.stateColor(theme, mouse, warning))

    def showParts(self, summary, history, paneMessage):
        for widget in (self.mouseIcon, self.nameLabel, self.levelLabel, self.statusLabel):
            widget.setVisible(summary)
        for widget in [self.healthTitle, self.symbol, self.segments, self.refreshLabel] + self.lineLabels:
            widget.setVisible(history)
        for index, chart in enumerate(self.charts):
            chart.setVisible(history and index == self.segments.index)
        self.rangeControl.setVisible(history and self.segments.index == 0)
        self.message.setVisible(summary and not history)
        for widget in self.formLabels + self.formValues + [self.popup]:
            widget.setVisible(summary)
        self.paneMessage.setVisible(paneMessage)
        self.panels = []
        if (summary):
            self.panels.append("form")
        if (history):
            self.panels += ["health", "segments", "chart"]

    def fillHealth(self, theme):
        lines = self.health.lines
        for index, widget in enumerate(self.lineLabels):
            widget.setText(lines[index].text if index < len(lines) else "")
            widget.setStyleSheet("color: " + (theme.text if index == 0 else theme.secondary))
            widget.setVisible(index < len(lines))
        self.symbol.setSymbol(self.health.symbol, theme)

    def fillCharts(self, summary, reading, key):
        voltage = reading is not None and reading.percent is None and reading.level is None and reading.millivolts is not None
        rough = reading is not None and reading.percent is None and reading.level is not None
        self.fillLevels(summary, key, voltage, rough)
        self.hoursChart.setSeries({"hours": self.health.hoursPerCharge})
        self.chargingChart.setSeries(self.health.chargingByPower)

    def fillLevels(self, summary, key, voltage, rough):
        rangeName = self.levelRange
        if (rangeName == "week"):
            # the history reply already holds the week, so the default range costs no call
            self.levelChart.setSlots(summary.get("recent") or [], voltage, rangeName, rough=rough)
            return
        slots = self.levels.get(key, {}).get(rangeName)
        error = self.levelErrors.get((key, rangeName))
        if (slots is not None):
            self.levelChart.setSlots(slots, voltage, rangeName, rough=rough)
        elif (error in RESTART_LEVEL_ERRORS):
            self.levelChart.setSlots([], voltage, rangeName, message=RANGE_RESTART_TEXT)
        elif (error is not None and error != serviceState.READ_ALL_LIMITED):
            self.levelChart.setSlots([], voltage, rangeName, message=RANGE_ERROR_TEXT)
        else:
            # set before the call, since a reply can come back inside it and fill the chart
            self.levelChart.setSlots([], voltage, rangeName, message=LOADING_TEXT)
        if (error is None and (key, rangeName) not in self.freshLevels):
            self.fetchLevels(key, rangeName)

    def fillForm(self, result):
        values = ("-", "-", "-") if result is None else (str(result.cycles), result.lastCharged or "-", result.chargingSpeed or "-")
        for widget, value in zip(self.formValues, values):
            widget.setText(value)

    def setGlow(self, color):
        color = QColor(glass.GRAY) if color is None else color
        if (color == self.glow and self.backdrop is not None):
            return
        duration = int(GLOW_FADE_MS * self.motionFactor)
        self.glowAnimation.stop()
        if (duration > 0 and self.isVisible() and self.backdrop is not None):
            self.glowAnimation.setStartValue(QColor(self.glow))
            self.glowAnimation.setEndValue(QColor(color))
            self.glowAnimation.setDuration(duration)
            self.frostClock.start()
            self.glowAnimation.start()
            return
        self.glow = QColor(color)
        self.remakeFrost(force=True)

    def onGlowStep(self, value):
        self.glow = QColor(value)
        self.remakeFrost()

    def remakeFrost(self, force=False):
        # while the glow fades the frost is made again at most every 50 ms, and once more at its end
        if (not force and self.frostClock.isValid() and self.frostClock.elapsed() < FROST_EVERY_MS):
            return
        self.frostClock.start()
        self.backdrop = None
        self.update()
        for child in (self.segments, self.rangeControl, self.popup, self.sidebar, self.choiceMenu):
            child.update()

    def paneRect(self):
        x = SIDEBAR_WIDTH if self.sidebar.isVisibleTo(self) else 0
        return QRectF(x, 0, self.width() - x, self.height())

    def ensureFrost(self):
        key = (self.width(), self.height(), self.glow.name(), self.theme.dark, self.devicePixelRatioF(), self.sidebar.isVisibleTo(self))
        if (self.backdrop is not None and key == self.frostKey):
            return
        self.backdrop = glass.paintBackdrop(self.width(), self.height(), self.paneRect(), self.glow, self.theme, self.devicePixelRatioF())
        self.frostImage = glass.frosted(self.backdrop)
        self.frostKey = key

    def healthHeight(self):
        width = HEALTH_WIDTH - 60
        height = 42 + sum(widget.heightForWidth(width) + 4 for widget in self.lineLabels if widget.isVisibleTo(self) and widget.text()) + 14
        if (self.refreshLabel.isVisibleTo(self) and self.refreshLabel.text()):
            height += self.refreshLabel.heightForWidth(width) + 4
        return max(HEALTH_MIN_HEIGHT, height)

    def layoutParts(self):
        sidebarWidth = SIDEBAR_WIDTH if self.sidebar.isVisibleTo(self) else 0
        healthHeight = self.healthHeight() if "health" in self.panels else HEALTH_MIN_HEIGHT
        top = max(TOP_ROW, healthHeight)
        wantedHeight = min(MAX_HEIGHT, FIXED_HEIGHT + top + CHART_HEIGHT)
        self.setMinimumSize(sidebarWidth + PANE_WIDTH, max(BASE_HEIGHT, wantedHeight))
        if (self.width() < sidebarWidth + PANE_WIDTH or self.height() < wantedHeight):
            self.resize(max(self.width(), sidebarWidth + PANE_WIDTH) if self.width() >= sidebarWidth + PANE_WIDTH else sidebarWidth + PANE_WIDTH, max(self.height(), wantedHeight))
        if (not self.sidebar.isVisibleTo(self) and self.width() > PANE_WIDTH and self.minimumWidth() == PANE_WIDTH and self.property("hadSidebar")):
            # the window shrinks back by the sidebar's width once the sidebar goes
            self.resize(self.width() - SIDEBAR_WIDTH, self.height())
        self.setProperty("hadSidebar", self.sidebar.isVisibleTo(self))
        self.sidebar.setGeometry(0, 0, SIDEBAR_WIDTH, self.height())
        width = self.width() - sidebarWidth
        self.pane.setGeometry(sidebarWidth, 0, width, self.height())
        self.mouseIcon.move(MARGIN - 6, MARGIN)
        textX = MARGIN + ICON_SIZE + 12
        self.nameLabel.setGeometry(textX, MARGIN + 10, 210, 18)
        self.levelLabel.setGeometry(textX - 2, MARGIN + 28, 220, 50)
        self.statusLabel.setGeometry(textX, MARGIN + 80, 210, 40)
        healthX = width - MARGIN - HEALTH_WIDTH
        self.healthRect = QRectF(healthX, MARGIN, HEALTH_WIDTH, healthHeight)
        self.healthTitle.setGeometry(int(healthX + 18), MARGIN + 12, 200, 18)
        self.symbol.move(int(healthX + 18), MARGIN + 40)
        y = MARGIN + 38
        for index, widget in enumerate(self.lineLabels):
            height = widget.heightForWidth(HEALTH_WIDTH - 60) if widget.text() else 0
            widget.setGeometry(int(healthX + 44), y, HEALTH_WIDTH - 60, max(0, height))
            y += height + 4 if height else 0
        self.refreshLabel.setGeometry(int(healthX + 44), y, HEALTH_WIDTH - 60, self.refreshLabel.heightForWidth(HEALTH_WIDTH - 60) if self.refreshLabel.text() else 0)
        segmentY = MARGIN + top + 20
        self.segments.move(int((width - self.segments.width()) / 2), segmentY)
        self.segmentRect = QRectF((width - self.segments.width()) / 2, segmentY, self.segments.width(), SEGMENT_HEIGHT)
        chartY = segmentY + SEGMENT_HEIGHT + 14
        chartHeight = max(CHART_MIN_HEIGHT, self.height() - chartY - 16 - FORM_HEIGHT - MARGIN)
        self.chartRect = QRectF(MARGIN, chartY, width - 2 * MARGIN, chartHeight)
        for chart in self.charts:
            chart.setGeometry(self.chartRect.toRect())
        self.rangeControl.move(int(self.chartRect.right() - 12 - self.rangeControl.width()), int(self.chartRect.y() + 6))
        self.message.setGeometry(MARGIN, MARGIN + TOP_ROW + 20, width - 2 * MARGIN, chartY + chartHeight - MARGIN - TOP_ROW - 20)
        formY = chartY + chartHeight + 16
        self.formRect = QRectF(MARGIN, formY, width - 2 * MARGIN, FORM_HEIGHT)
        half = (width - 2 * MARGIN) / 2
        for index, widget in enumerate(self.formLabels):
            column, row = index // 2, index % 2
            # the setting's label leaves room for its button
            spare = self.popup.width() + 8 if index == 3 else 0
            widget.setGeometry(int(MARGIN + column * half + 18), formY + row * 36, int(half - 36 - spare), 36)
        for index, widget in enumerate(self.formValues):
            column, row = index // 2, index % 2
            widget.setGeometry(int(MARGIN + column * half + 18), formY + row * 36, int(half - 36), 36)
        self.popup.move(int(MARGIN + 2 * half - 18 - self.popup.width()), formY + 36 + 6)
        self.paneMessage.setGeometry(MARGIN, MARGIN, width - 2 * MARGIN, self.height() - 2 * MARGIN)

    def resizeEvent(self, event):
        self.choiceMenu.hide()
        self.layoutParts()
        super().resizeEvent(event)

    def paintEvent(self, event):
        self.ensureFrost()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if (self.blurred):
            # the desktop shows through, blurred by KWin, under the glows at three quarters
            painter.setOpacity(0.75)
        painter.drawImage(QRectF(0, 0, self.width(), self.height()), self.backdrop)
        painter.setOpacity(1.0)
        theme = self.theme
        if (self.sidebar.isVisibleTo(self)):
            sidebar = QRectF(8, 8, SIDEBAR_WIDTH - 16, self.height() - 16)
            glass.paintShadow(painter, sidebar, 18, theme)
            glass.paintPanel(painter, sidebar, 18, self.frostImage, theme)
        offset = SIDEBAR_WIDTH if self.sidebar.isVisibleTo(self) else 0
        rects = {"health": (getattr(self, "healthRect", None), 18), "segments": (getattr(self, "segmentRect", None), SEGMENT_HEIGHT / 2), "chart": (getattr(self, "chartRect", None), 18), "form": (getattr(self, "formRect", None), 18)}
        for name in self.panels:
            rect, radius = rects[name]
            if (rect is None):
                continue
            placed = rect.translated(offset, 0)
            glass.paintShadow(painter, placed, radius, theme)
            glass.paintPanel(painter, placed, radius, self.frostImage, theme)
        if ("form" in self.panels and getattr(self, "formRect", None) is not None):
            form = self.formRect.translated(offset, 0)
            separator = glass.rgba(theme.grid)
            painter.fillRect(QRectF(form.x() + 18, form.y() + 36, form.width() / 2 - 36, 1), separator)
            painter.fillRect(QRectF(form.x() + form.width() / 2 + 18, form.y() + 36, form.width() / 2 - 36, 1), separator)
            painter.fillRect(QRectF(form.x() + form.width() / 2, form.y() + 12, 1, form.height() - 24), separator)
