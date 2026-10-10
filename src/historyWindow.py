import ctypes
import math
import os
import time

from PyQt6 import sip
from PyQt6.QtCore import QAbstractAnimation, QElapsedTimer, QPoint, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, QVariantAnimation, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusMessage
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QScrollBar, QWidget

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
# the narrowest pane that keeps the name and status clear of the health box
PANE_MIN_WIDTH = MARGIN + ICON_SIZE + 12 + 120 + 12 + HEALTH_WIDTH + MARGIN
# a usual title bar, for the room the screen leaves before the window has shown and its frame is known
FRAME_ALLOWANCE = 40
FETCH_GAP_SECONDS = 30
# the service writes a UPower reading once its wake set wait is over, and holds a change of the percent alone until HOLD_SECONDS after its last write, so a fetch after a change waits out both and a bit for the GetAll each side makes
UPOWER_SETTLE_SECONDS = history.WAKE_SET_SECONDS + history.HOLD_SECONDS + 3
STALE_SLOT_SECONDS = 2 * 60 * 60
# a reply's newest slot ends at the moment the service answered, so one this far past the clock means the clock was set back since
CLOCK_BACK_SECONDS = 5 * 60
RETRY_MS = 30 * 1000
# KWin answered within 20 ms where it was measured, so the first check comes soon and a slower machine gets one more
BLUR_CHECK_MS = (25, 100)
CHART_FADE_MS = 200
PANE_FADE_MS = 200
# a reply from the service usually comes well inside this, so most range switches never show the word at all
LOADING_DELAY_MS = 300
THUMB_SLIDE_MS = 250
MENU_OPEN_MS = 150
MENU_CLOSE_MS = 120
MENU_START_SCALE = 0.97
GLOW_FADE_MS = 400
BAR_MORPH_MS = 250
FROST_EVERY_MS = 50
# a window shown again keeps last time's bars and glow, so the replies that land as it appears change them at once instead of moving them
OPEN_QUIET_MS = 2000
SLEEP_CHOICES = ((0, "Off"), (30, "Every 30 seconds"), (60, "Every minute"), (300, "Every 5 minutes"))
# the button shows the choice short, so the label beside it fits, while its menu names each one in full
SLEEP_SHORT = {0: "Off", 30: "30 s", 60: "1 min", 300: "5 min"}
SLEEP_LABEL = "Check whether mice are asleep"
CHOICE_ROW_HEIGHT = 26
CHOICE_PADDING = 5
CHOICE_TEXT_X = 26
CHOICE_RADIUS = 10
CHOICE_MARGIN = 8
CHOICE_GAP = 4
SEGMENTS = ("Level", "Hours per Charge", "Charging")
LOADING_TEXT = "Loading"
NO_SERVICE_TEXT = "Can't reach the battery service."
NO_SERIAL_TEXT = "This mouse doesn't give a serial number, so its history can't be kept."
UNAVAILABLE_TEXT = "Its history isn't available right now."
RESTART_TEXT = "Restart Mouse Battery to see its history."
RANGE_RESTART_TEXT = "Restart Mouse Battery to see this range."
RANGE_ERROR_TEXT = "Couldn't load this range."
RANGE_NAMES = ("day", "week", "month")
RANGE_LABELS = ("D", "W", "M")
RANGE_WIDTHS = (30, 30, 30)
RANGE_HEIGHT = 22
# an older service answers these the same way every time, so asking again changes nothing
RESTART_LEVEL_ERRORS = (serviceState.UNKNOWN_METHOD, serviceState.ACCESS_DENIED, serviceState.INVALID_ARGS)
FINAL_ERRORS = RESTART_LEVEL_ERRORS + (serviceClient.UNDECODABLE,)
# a refused range waits out the refill of its budget before it's asked for again
LEVEL_RETRY_MS = serviceState.LEVELS_REFILL_SECONDS * 1000
NOT_REFRESHED_TEXT = "Couldn't refresh the history."
RANGE_NOT_REFRESHED_TEXT = "Couldn't refresh this range."


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
    # a device supplies the mouse's name, and its angle brackets would otherwise be read as rich text
    widget.setTextFormat(Qt.TextFormat.PlainText)
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
        # the percent and level the history ends on, which are None after a gap or in a history with no rows yet
        self.newestLevel = (recent[-1][1], recent[-1][2]) if recent else (None, None)


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
        self.state = None
        self.base = None
        self.setFixedSize(ICON_SIZE, ICON_SIZE)

    def setState(self, state, theme):
        self.state = state
        self.base = theme.iconBase
        self.render()
        self.update()

    def render(self):
        if (self.state is None):
            self.image = None
            return
        ratio = self.devicePixelRatioF()
        # drawn at the screen's pixel ratio, so it stays sharp on a HiDPI screen
        self.image = icon.renderImage(self.state, round(ICON_SIZE * ratio), self.base, number=False)
        self.image.setDevicePixelRatio(ratio)

    def paintEvent(self, event):
        # a window moved to a screen with another scale gets the icon drawn again for it
        if (self.image is not None and self.image.devicePixelRatio() != self.devicePixelRatioF()):
            self.render()
        if (self.image is not None):
            QPainter(self).drawImage(QRectF(0, 0, ICON_SIZE, ICON_SIZE), self.image)


def isKeyFocus(event):
    # only Tab and Shift+Tab draw a ring, since the window hands its first control focus when it's activated and a click shouldn't leave one either
    return event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)


def paintFocusRing(painter, rect, radius):
    # the controls are painted, so each draws the keyboard focus itself, in the blue the glass uses for its highlight
    painter.setPen(QPen(QColor(glass.BLUE), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)


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
        self.barHeight = height
        self.index = index
        self.thumb = self.segmentRect(index)
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(glass.easeOut())
        self.animation.valueChanged.connect(self.onSlide)
        self.setFixedSize(int(sum(widths) + 2 * self.inset), height)
        # Tab reaches it, while a click leaves no focus ring behind
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.keyFocus = False

    def segmentX(self, index):
        return self.inset + sum(self.widths[:index])

    def segmentRect(self, index):
        return QRectF(self.segmentX(index), self.inset, self.widths[index], self.barHeight - 2 * self.inset)

    def choose(self, index, animate=True):
        if (index == self.index):
            return
        self.index = index
        duration = int(THUMB_SLIDE_MS * self.owner.motionFactor)
        self.animation.stop()
        if (animate and duration > 0):
            self.animation.setStartValue(QRectF(self.thumb))
            self.animation.setEndValue(self.segmentRect(index))
            self.animation.setDuration(duration)
            self.animation.start()
        else:
            self.thumb = self.segmentRect(index)
            self.update()
        self.chosen.emit(index)

    def onSlide(self, value):
        self.thumb = QRectF(value)
        self.update()

    def underThumb(self):
        middle = self.thumb.center().x()
        return next((index for index in range(len(self.names)) if self.segmentX(index) <= middle < self.segmentX(index + 1)), self.index)

    def mousePressEvent(self, event):
        if (event.button() != Qt.MouseButton.LeftButton):
            return
        x = event.position().x()
        for index in range(len(self.names)):
            if (self.segmentX(index) <= x < self.segmentX(index + 1)):
                self.choose(index)
                return

    def focusInEvent(self, event):
        self.keyFocus = isKeyFocus(event)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.keyFocus = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if (key == Qt.Key.Key_Left):
            self.choose(max(0, self.index - 1))
        elif (key == Qt.Key.Key_Right):
            self.choose(min(len(self.names) - 1, self.index + 1))
        elif (key == Qt.Key.Key_Home):
            self.choose(0)
        elif (key == Qt.Key.Key_End):
            self.choose(len(self.names) - 1)
        else:
            super().keyPressEvent(event)

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
            thumb = QRectF(self.thumb)
            glass.paintPanel(painter, thumb, thumb.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
        bold = self.underThumb()
        for index, name in enumerate(self.names):
            painter.setFont(font(self.fontSize, QFont.Weight.DemiBold if index == bold else QFont.Weight.Normal))
            painter.setPen(QColor(theme.text))
            painter.drawText(QRectF(self.segmentX(index), 0, self.widths[index], self.height()), int(Qt.AlignmentFlag.AlignCenter), name)
        if (self.hasFocus() and self.keyFocus):
            paintFocusRing(painter, QRectF(self.rect()), self.height() / 2)


class ChoiceMenu(QWidget):
    # drawn inside the window instead of as a QMenu, so its glass comes from the same frost as the panels around it
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.choices = ()
        self.marked = 0
        self.hover = 0
        self.onPick = None
        self.onReopen = None
        self.anchor = QRectF()
        self.pressed = False
        self.returnTo = None
        self.returnReason = Qt.FocusReason.OtherFocusReason
        self.panel = QRectF()
        self.progress = 1.0
        self.origin = QPointF()
        self.closing = False
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(glass.easeOut())
        self.animation.valueChanged.connect(self.onProgress)
        self.animation.finished.connect(self.settle)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(SLEEP_LABEL)
        self.hide()

    def open(self, choices, value, anchor, onPick, onReopen):
        self.choices = choices
        self.marked = next((index for index, (choice, name) in enumerate(choices) if choice == value), 0)
        self.setHover(self.marked)
        self.onPick = onPick
        self.onReopen = onReopen
        self.anchor = anchor
        self.pressed = False
        self.setGeometry(self.owner.rect())
        width = max(anchor.width(), CHOICE_TEXT_X + max(QFontMetricsF(font(13)).horizontalAdvance(name) for choice, name in choices) + CHOICE_PADDING * 3)
        height = len(choices) * CHOICE_ROW_HEIGHT + 2 * CHOICE_PADDING
        # the marked row opens over the button, the way a macOS pop up button does, kept inside the window so the frost is under all of it
        top = anchor.center().y() - CHOICE_PADDING - self.marked * CHOICE_ROW_HEIGHT - CHOICE_ROW_HEIGHT / 2
        top = min(max(top, CHOICE_MARGIN), self.height() - CHOICE_MARGIN - height)
        left = min(max(anchor.right() - width, CHOICE_MARGIN), self.width() - CHOICE_MARGIN - width)
        self.panel = QRectF(left, top, width, height)
        if (any(index != self.marked and self.rowRect(index).intersects(anchor) for index in range(len(choices)))):
            # kept inside the window the panel put another choice under part of the button, where a second click would pick it, so it opens above the button instead
            self.panel.moveBottom(max(anchor.top() - CHOICE_GAP, CHOICE_MARGIN + height))
        # closing from the keys gives the focus back where it was, so Tab carries on from the button
        # a reopen during a close finds the focus on the menu already, so the old place is kept
        focused = self.owner.focusWidget()
        if (focused is not self):
            self.returnTo = focused
            self.returnReason = Qt.FocusReason.TabFocusReason if getattr(focused, "keyFocus", False) else Qt.FocusReason.OtherFocusReason
        self.origin = anchor.center()
        self.closing = False
        # reopening mid close grows back from where the fade got to
        start = self.progress if self.isVisible() and self.progress < 1.0 else 0.0
        self.show()
        self.raise_()
        self.setFocus()
        duration = int(MENU_OPEN_MS * self.owner.motionFactor)
        self.animation.stop()
        if (duration > 0):
            self.animation.setStartValue(start)
            self.animation.setEndValue(1.0)
            self.animation.setDuration(duration)
            self.progress = start
            self.animation.start()
        else:
            self.progress = 1.0
        self.update()

    def onProgress(self, value):
        self.progress = float(value)
        # the menu covers the whole window, so a frame repaints only what the layer can reach instead of everything under it
        self.update(self.motionRect().toAlignedRect())

    def layerRect(self):
        # the panel and its shadow, which spreads past the panel and drops below it
        grow = max(spread for spread, alpha in glass.SHADOWS) + glass.SHADOW_DROP
        return QRectF(self.panel.adjusted(-grow, -grow, grow, grow).toAlignedRect())

    def motionRect(self):
        # the layer scales towards the button's center, so the smallest frame can reach past the layer's own rect
        layer = self.layerRect()
        small = QRectF(self.origin.x() + (layer.x() - self.origin.x()) * MENU_START_SCALE, self.origin.y() + (layer.y() - self.origin.y()) * MENU_START_SCALE, layer.width() * MENU_START_SCALE, layer.height() * MENU_START_SCALE)
        return layer.united(small)

    def settle(self):
        if (self.closing):
            self.closing = False
            self.hide()

    def setHover(self, index):
        self.hover = index
        # the rows are painted, so the menu itself tells a screen reader which choice the keys are on
        self.setAccessibleDescription(self.choices[index][1])
        self.update()

    def rowRect(self, index):
        return QRectF(self.panel.x() + CHOICE_PADDING, self.panel.y() + CHOICE_PADDING + index * CHOICE_ROW_HEIGHT, self.panel.width() - 2 * CHOICE_PADDING, CHOICE_ROW_HEIGHT)

    def rowAt(self, point):
        return next((index for index in range(len(self.choices)) if self.rowRect(index).contains(point)), None)

    def choose(self, index):
        self.dismiss()
        self.onPick(self.choices[index][0])

    def dismiss(self):
        if (self.closing):
            return
        duration = int(MENU_CLOSE_MS * self.owner.motionFactor)
        if (duration > 0 and self.isVisible()):
            self.closing = True
            self.animation.stop()
            self.animation.setStartValue(self.progress)
            self.animation.setEndValue(0.0)
            self.animation.setDuration(duration)
            self.progress = self.animation.startValue()
            self.animation.start()
        else:
            self.hide()

    def hideEvent(self, event):
        # a minimize that leaves the focus on the menu sends it a spontaneous hide without hiding it, so the fade carries on, while on KDE the window loses the focus first and focusOutEvent has hidden the menu already
        if (not event.spontaneous()):
            self.animation.stop()
            self.progress = 1.0
            self.closing = False
        # a hidden widget that held focus would pass it to the next control, so it goes back to where it came from, ring and all, before Qt does that
        if (self.owner.focusWidget() is self):
            if (self.returnTo is not None):
                self.returnTo.setFocus(self.returnReason)
            else:
                self.clearFocus()
        super().hideEvent(event)

    def mouseMoveEvent(self, event):
        if (self.closing):
            return
        index = self.rowAt(event.position())
        if (index is not None and index != self.hover):
            self.setHover(index)

    def mousePressEvent(self, event):
        if (self.closing):
            if (event.button() == Qt.MouseButton.LeftButton and self.anchor.contains(event.position())):
                self.onReopen()
            return
        # a press of any button away from the panel closes the menu, while only the left one picks
        if (not self.panel.contains(event.position())):
            self.dismiss()
            return
        if (event.button() != Qt.MouseButton.LeftButton):
            return
        self.pressed = True

    def mouseDoubleClickEvent(self, event):
        # the only double click an open menu gets is the second half of the one on the button that opened it
        pass

    def mouseReleaseEvent(self, event):
        if (self.closing or event.button() != Qt.MouseButton.LeftButton):
            return
        # a release picks only after a press inside the open menu, so the release that ends a double click on the button never does
        if (not self.pressed):
            return
        self.pressed = False
        index = self.rowAt(event.position())
        if (index is not None and self.isVisible()):
            self.choose(index)

    def keyPressEvent(self, event):
        if (self.closing):
            return
        key = event.key()
        if (key == Qt.Key.Key_Escape):
            self.dismiss()
        elif (key in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.setHover(max(0, min(len(self.choices) - 1, self.hover + (1 if key == Qt.Key.Key_Down else -1))))
        elif (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)):
            self.choose(self.hover)

    def focusOutEvent(self, event):
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if (self.progress >= 1.0):
            self.paintMenu(painter)
            return
        # the menu opens over its button, so it grows out of the button instead of its own center
        scale = MENU_START_SCALE + (1.0 - MENU_START_SCALE) * self.progress
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self.progress)
        painter.translate(self.origin)
        painter.scale(scale, scale)
        painter.translate(-self.origin)
        painter.drawPixmap(self.layerRect().topLeft(), self.paintLayer())

    def paintLayer(self):
        # drawn whole at full opacity first, so the panel doesn't show its own shadow through itself while it fades
        rect = self.layerRect()
        ratio = self.devicePixelRatioF()
        layer = QPixmap(math.ceil(rect.width() * ratio), math.ceil(rect.height() * ratio))
        layer.setDevicePixelRatio(ratio)
        layer.fill(Qt.GlobalColor.transparent)
        painter = QPainter(layer)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(-rect.topLeft())
        self.paintMenu(painter)
        painter.end()
        return layer

    def paintMenu(self, painter):
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
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.keyFocus = False

    def setSeconds(self, seconds):
        self.seconds = seconds
        # painted instead of a QComboBox, so a screen reader gets its name and value from here
        self.setAccessibleName(SLEEP_LABEL + ", " + self.text())
        self.update()

    def text(self):
        return SLEEP_SHORT.get(self.seconds, "Off")

    def mousePressEvent(self, event):
        if (event.button() != Qt.MouseButton.LeftButton):
            return
        self.openMenu()

    def focusInEvent(self, event):
        self.keyFocus = isKeyFocus(event)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.keyFocus = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.openMenu()
        else:
            super().keyPressEvent(event)

    def openMenu(self):
        anchor = QRectF(QPointF(self.mapTo(self.owner, QPoint(0, 0))), QSizeF(self.size()))
        self.owner.choiceMenu.open(SLEEP_CHOICES, self.seconds, anchor, self.pick, self.openMenu)

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
        if (self.hasFocus() and self.keyFocus):
            paintFocusRing(painter, QRectF(self.rect()), self.height() / 2)


def pictureOpacity(progress):
    return max(0.0, 1.0 - progress / 0.6)


class Crossfade(QWidget):
    # a picture of what was showing, laid over its place while the new content fades in, so a switch hands over instead of blinking through an empty panel
    def __init__(self, window, parent):
        super().__init__(parent)
        self.owner = window
        self.picture = None
        # the share of the time gone, which the picture fades on, since the eased progress passes 0.6 at about a sixth of the time and a switch would read as a fade up instead of a handover
        self.elapsed = 1.0
        self.fade = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(glass.easeOut())
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self.onStep)
        self.animation.finished.connect(self.done)
        self.hide()

    def finish(self):
        if (self.animation.state() == QAbstractAnimation.State.Running):
            self.animation.setCurrentTime(self.animation.duration())

    def start(self, source, durationMs, fade):
        # fade is the QGraphicsOpacityEffect on the incoming content, stepped with the picture
        self.finish()
        self.fade = fade
        if (durationMs <= 0 or not source.isVisible()):
            fade.setEnabled(False)
            return
        self.picture = source.grab()
        self.setGeometry(source.geometry())
        self.elapsed = 0.0
        fade.setOpacity(0.0)
        fade.setEnabled(True)
        self.raise_()
        self.show()
        self.animation.setDuration(durationMs)
        self.animation.start()

    def onStep(self, value):
        self.elapsed = self.animation.currentTime() / max(1, self.animation.duration())
        self.fade.setOpacity(value)
        self.update()

    def done(self):
        self.fade.setEnabled(False)
        self.picture = None
        self.hide()

    def paintEvent(self, event):
        if (self.picture is None):
            return
        painter = QPainter(self)
        painter.setOpacity(pictureOpacity(self.elapsed))
        painter.drawPixmap(0, 0, self.picture)


class Sidebar(QWidget):
    picked = pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.rows = []
        self.selected = None
        self.pill = None
        self.pillAnimation = QVariantAnimation(self)
        self.pillAnimation.setEasingCurve(glass.easeOut())
        self.pillAnimation.valueChanged.connect(self.onPillSlide)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.keyFocus = False

    def onPillSlide(self, value):
        self.pill = QRectF(value)
        self.update()

    def slidePill(self, rows, selected):
        index = next((i for i, row in enumerate(rows) if row[0] == selected), None)
        if (index is None):
            self.pillAnimation.stop()
            self.pill = None
            return
        target = self.rowRect(index)
        # new text for the mouse already being slid to must not restart the slide
        if (self.pillAnimation.state() == QAbstractAnimation.State.Running and self.pillAnimation.endValue() == target):
            return
        duration = int(THUMB_SLIDE_MS * self.owner.motionFactor)
        gone = not any(row[0] == self.selected for row in rows)
        self.pillAnimation.stop()
        if (self.pill is None or gone or selected == self.selected or not self.isVisible() or duration <= 0):
            self.pill = target
            return
        self.pillAnimation.setStartValue(QRectF(self.pill))
        self.pillAnimation.setEndValue(target)
        self.pillAnimation.setDuration(duration)
        self.pillAnimation.start()

    def shownPill(self):
        if (self.pill is not None):
            return self.pill
        index = self.selectedIndex()
        return self.rowRect(index) if index is not None else None

    def resizeEvent(self, event):
        index = self.selectedIndex()
        if (self.pill is not None and index is not None and event.size().width() != event.oldSize().width()):
            self.pillAnimation.stop()
            self.pill = self.rowRect(index)
        super().resizeEvent(event)

    def hideEvent(self, event):
        # a pill left behind would slide from a stale row when the sidebar comes back
        self.pillAnimation.stop()
        self.pill = None
        super().hideEvent(event)

    def setRows(self, rows, selected):
        # each row is (key, name, second line, capsule fill from 0 to 1 or None, color name of the second line)
        self.slidePill(rows, selected)
        self.rows = rows
        self.selected = selected
        # painted instead of a list view, so a screen reader gets the chosen mouse and its line from here
        chosen = next((row for row in rows if row[0] == selected), None)
        self.setAccessibleName("Mice" if chosen is None else "Mice, " + chosen[1] + ", " + chosen[2])
        self.update()

    def rowRect(self, index):
        return QRectF(10, 40 + index * (ROW_HEIGHT + 4), self.width() - 20, ROW_HEIGHT)

    def mousePressEvent(self, event):
        if (event.button() != Qt.MouseButton.LeftButton):
            return
        for index, row in enumerate(self.rows):
            if (self.rowRect(index).contains(event.position())):
                self.picked.emit(row[0])
                return

    def focusInEvent(self, event):
        self.keyFocus = isKeyFocus(event)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.keyFocus = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if (key not in (Qt.Key.Key_Up, Qt.Key.Key_Down) or not self.rows):
            super().keyPressEvent(event)
            return
        index = self.selectedIndex()
        if (index is None):
            index = 0
        elif (key == Qt.Key.Key_Up):
            index = max(0, index - 1)
        else:
            index = min(len(self.rows) - 1, index + 1)
        self.picked.emit(self.rows[index][0])

    def selectedIndex(self):
        return next((index for index, row in enumerate(self.rows) if row[0] == self.selected), None)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        painter.setFont(font(11, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.secondary))
        painter.drawText(QRectF(20, 12, 200, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "Mice")
        frost = self.owner.frost()
        pill = self.shownPill()
        if (pill is not None and frost is not None):
            glass.paintPanel(painter, pill, pill.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
        for index, (key, name, second, fill, secondColor) in enumerate(self.rows):
            rect = self.rowRect(index)
            self.paintCapsule(painter, QRectF(rect.x() + 12, rect.y() + 10, 22, 11), fill, theme, secondColor)
            painter.setFont(font(13, QFont.Weight.Medium))
            painter.setPen(QColor(theme.text))
            painter.drawText(QRectF(rect.x() + 44, rect.y() + 4, rect.width() - 52, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(rect.width() - 52)))
            painter.setFont(font(11))
            painter.setPen(QColor(secondColor))
            painter.drawText(QRectF(rect.x() + 44, rect.y() + 23, rect.width() - 52, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), second)
        if (self.hasFocus() and self.keyFocus):
            index = self.selectedIndex()
            ring = self.rowRect(index) if index is not None else QRectF(self.rect())
            paintFocusRing(painter, ring, ring.height() / 2 if index is not None else 12)

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
        self.watchedHandle = None
        self.watchedScreen = None
        self.frostClock = QElapsedTimer()
        self.openedClock = QElapsedTimer()
        self.replies = {}
        self.errors = {}
        self.notRefreshed = set()
        # ranges whose reload failed while their old bars stayed up, until one answers
        self.levelReloadFailed = set()
        # each call still out by a number of its own, so a cache forgotten while one was out can't take its answer back in
        self.pending = {}
        self.callCount = 0
        self.lastFetch = {}
        self.upowerChangedAt = {}
        self.upowerSeen = {}
        # the reading each UPower mouse was last asked for again, so a service that never saves it costs one more ask instead of one every gap
        self.upowerAskedAgain = {}
        # the tray hears UPower before the window is built, so a refresh that leaves the reading alone isn't taken for a change
        for mouse in mouseList.mice:
            if (mouse.upowerPath is not None):
                self.upowerSeen[mouse.key] = (mouse.upowerPath, mouse.reading)
        self.levelRange = levelRange if levelRange in RANGE_NAMES else "week"
        # each mouse's slots per range, dropped with its history reply, since a new reply means new readings
        self.levels = {}
        # the ranges asked for since each mouse's last history reply, while older slots stay drawn until their new answer
        self.freshLevels = set()
        self.levelErrors = {}
        self.pendingLevels = {}
        self.selected = None
        self.wanted = None
        self.health = None
        self.stateIconName = None
        self.panels = []
        self.scrollX = 0
        self.scrollY = 0
        self.makeParts(sleepCheckSeconds)
        self.fetchTimer = mice.singleShotTimer(self, 1000, self.fetchSelected)
        self.retryTimer = mice.singleShotTimer(self, RETRY_MS, self.fetchSelected)
        self.levelRetryTimer = mice.singleShotTimer(self, LEVEL_RETRY_MS, self.retryLevels)
        self.loadingTimer = mice.singleShotTimer(self, LOADING_DELAY_MS, self.showLoading)
        # the mouse the level chart's content belongs to, and the (mouse, range) the loading delay already ran out for
        self.levelChartKey = None
        self.levelChartWasShown = False
        self.pickingRange = False
        self.loadingShown = None
        self.blurTimer = mice.singleShotTimer(self, BLUR_CHECK_MS[0], self.checkBlur)
        self.blurChecks = 0
        self.ageTimer = mice.singleShotTimer(self, 1000, self.fetchSelected)
        self.glowAnimation = QVariantAnimation(self)
        self.glowAnimation.setEasingCurve(glass.easeOut())
        self.glowAnimation.valueChanged.connect(self.onGlowStep)
        self.glowAnimation.finished.connect(lambda: self.remakeFrost(force=True))
        mouseList.changed.connect(self.refresh)
        serviceWatcher.serviceRestarted.connect(self.forgetReplies)
        if (upowerWatcher is not None):
            upowerWatcher.mouseChanged.connect(self.onUPowerMouse)
            upowerWatcher.mouseRemoved.connect(self.onUPowerRemoved)
        QApplication.instance().paletteChanged.connect(self.onPaletteChanged)
        QApplication.instance().focusChanged.connect(self.revealFocus)
        self.watchingPortal = False
        self.portalChanges = {"color-scheme": 0, "reduced-motion": 0}
        self.blurred = False

    def makeParts(self, sleepCheckSeconds):
        self.sidebar = Sidebar(self)
        self.sidebar.picked.connect(self.pick)
        self.pane = QWidget(self)
        self.pane.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # the pane clips and everything in it lives on the content, which slides under the pane when the screen has less room than the layout needs
        self.content = QWidget(self.pane)
        self.content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # on the content instead of the pane, since an effect draws its children too and the old picture is a child of the pane
        self.paneFade = QGraphicsOpacityEffect(self.content)
        self.paneFade.setOpacity(1.0)
        # an opacity effect draws its widget from a cached image, and with the chart's own fade inside it a changed chart never showed, so each fade is only on while it runs
        self.paneFade.setEnabled(False)
        self.content.setGraphicsEffect(self.paneFade)
        self.paneCrossfade = Crossfade(self, self.pane)
        self.vScroll = QScrollBar(Qt.Orientation.Vertical, self)
        self.hScroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self.vScroll.valueChanged.connect(lambda value: self.scrollTo(self.scrollX, value))
        self.hScroll.valueChanged.connect(lambda value: self.scrollTo(value, self.scrollY))
        self.vScroll.hide()
        self.hScroll.hide()
        self.mouseIcon = MouseIcon(self.content)
        self.nameLabel = label(self.content, 12, QFont.Weight.Medium)
        self.levelLabel = label(self.content, 40, QFont.Weight.Bold)
        self.statusLabel = label(self.content, 13, wrap=True)
        self.healthTitle = label(self.content, 12, QFont.Weight.Medium)
        self.healthTitle.setText("Battery Health")
        self.symbol = HealthSymbol(self.content)
        self.lineLabels = [label(self.content, 13 if index == 0 else 12, QFont.Weight.DemiBold if index == 0 else QFont.Weight.Normal, wrap=True) for index in range(3)]
        self.refreshLabel = label(self.content, 11, wrap=True)
        self.segments = SegmentedControl(self)
        self.segments.setParent(self.content)
        self.segments.chosen.connect(self.showChart)
        self.nameSegments()
        self.levelChart = charts.LevelChart(self.content)
        self.hoursChart = charts.LineChart("Hours per Charge", "h", charts.HOURS_EMPTY, "h", self.content)
        self.chargingChart = charts.LineChart("Charging Speed", "min per 10%", charts.CHARGING_EMPTY, "min", self.content, byCharger=True)
        self.charts = [self.levelChart, self.hoursChart, self.chargingChart]
        # made after the charts, so it sits above the level chart's corner
        self.rangeControl = SegmentedControl(self, RANGE_LABELS, RANGE_WIDTHS, RANGE_HEIGHT, 11, track=True, index=RANGE_NAMES.index(self.levelRange))
        self.rangeControl.setParent(self.content)
        self.rangeControl.chosen.connect(lambda index: self.pickRange(RANGE_NAMES[index]))
        self.nameRangeControl()
        self.chartFade = QGraphicsOpacityEffect(self.levelChart)
        self.chartCrossfade = Crossfade(self, self.content)
        self.rangeControl.raise_()
        self.formLabels = [label(self.content, 13) for index in range(4)]
        self.formValues = [label(self.content, 13) for index in range(3)]
        for widget, text in zip(self.formLabels, ("Charge cycles", "Last charged", "Charging speed", "Check whether mice are asleep")):
            widget.setText(text)
        for widget in self.formValues:
            widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.popup = PopupButton(self)
        self.popup.setParent(self.content)
        self.popup.setSeconds(sleepCheckSeconds)
        self.popup.picked.connect(self.sleepCheckChanged.emit)
        self.choiceMenu = ChoiceMenu(self)
        self.message = label(self.content, 13, wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paneMessage = label(self.pane, 13, wrap=True)
        self.paneMessage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for chart in self.charts[1:]:
            chart.hide()

    def frost(self):
        return self.frostImage

    def open(self):
        self.motionFactor = glass.animationFactor(self.configHome, self.reducedMotion == 1)
        self.readPortal()
        # shown before the refresh, since the window only asks for a history while it's visible
        if (not self.isVisible()):
            self.openedClock.start()
            # a window opened from the tray starts on the tray's mouse
            self.selected = None
            self.wanted = None
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.show()
            self.watchScreen()
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
        # a minimized window still counts as visible, and neither show, raise_, nor activateWindow brings it back
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def isQuiet(self):
        return self.openedClock.isValid() and not self.openedClock.hasExpired(OPEN_QUIET_MS)

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
        for key in ("color-scheme", "reduced-motion"):
            changes = self.portalChanges[key]
            panelColor.askPortalSetting(self.sessionBus, self, key, lambda value, error, key=key, changes=changes: self.onPortalRead(key, value, error, changes))

    def onPortalRead(self, key, value, error, changes):
        # a SettingChanged that came while the read was out is newer than its answer
        if (changes != self.portalChanges[key]):
            return
        # a read that timed out says nothing about the setting, while a portal older than the key answers with an error that does, and then motion follows AnimationDurationFactor alone
        if (error is None or error in (panelColor.NOT_FOUND, panelColor.UNKNOWN_METHOD)):
            self.setPortal(key, value if error is None else 0)

    @pyqtSlot(QDBusMessage)
    def onPortalSetting(self, message):
        arguments = message.arguments()
        if (len(arguments) >= 3 and arguments[0] == panelColor.APPEARANCE and arguments[1] in ("color-scheme", "reduced-motion")):
            value = dbusCalls.plain(arguments[2])
            self.portalChanges[arguments[1]] += 1
            self.setPortal(arguments[1], value if isinstance(value, int) else 0)

    def setPortal(self, key, value):
        if (key == "color-scheme"):
            self.portalScheme = value
            self.applyTheme()
        else:
            self.reducedMotion = value
            self.motionFactor = glass.animationFactor(self.configHome, value == 1)
            if (self.motionFactor == 0.0):
                self.finishAnimations()

    def finishAnimations(self):
        for animation in (self.glowAnimation, self.paneCrossfade.animation, self.chartCrossfade.animation, self.levelChart.morph, self.segments.animation, self.rangeControl.animation, self.sidebar.pillAnimation, self.choiceMenu.animation):
            if (animation.state() == QAbstractAnimation.State.Running):
                animation.setCurrentTime(animation.duration())

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
        self.upowerAskedAgain.clear()
        # an answer still on its way comes from the old service, so it's set aside
        self.pending.clear()
        # the new service writes a UPower mouse's first row after its hold, so the first fetch waits as it does after a UPower change
        now = time.monotonic()
        for key in self.upowerSeen:
            self.upowerChangedAt[key] = now
        self.forgetLevels()
        self.refresh()

    def onUPowerMouse(self, upowerMouse):
        seen = self.upowerSeen.get(upowerMouse.key)
        # UPower refreshes its other properties too, and a reading that stayed the same gives the service nothing new to write
        if (seen is not None and seen[1] == upowerMouse.reading):
            return
        self.upowerSeen[upowerMouse.key] = (upowerMouse.path, upowerMouse.reading)
        self.upowerAskedAgain.pop(upowerMouse.key, None)
        # kept by key, so an answer to a fetch still on its way when the change came is old too
        self.upowerChangedAt[upowerMouse.key] = time.monotonic()
        if (upowerMouse.key == self.selected):
            self.refresh()

    def onUPowerRemoved(self, path):
        # the service writes a gap as the mouse goes, and the first reading once it's back, so that reading counts as a change
        self.upowerSeen = {key: seen for key, seen in self.upowerSeen.items() if seen[0] != path}

    def pick(self, key):
        if (key == self.selected):
            return
        self.selected = key
        self.loadingTimer.stop()
        self.loadingShown = None
        duration = int(PANE_FADE_MS * self.motionFactor) if self.isVisible() else 0
        self.paneCrossfade.start(self.content, duration, self.paneFade)
        # settled after the pane's picture, so that picture keeps the chart as it stood partway through its own fade
        self.chartCrossfade.finish()
        self.refresh()

    def nameSegments(self):
        self.segments.setAccessibleName("Chart, " + SEGMENTS[self.segments.index])

    def nameRangeControl(self):
        self.rangeControl.setAccessibleName("Chart range, " + charts.RANGE_TITLES[self.levelRange].split(", ", 1)[1])

    def pickRange(self, rangeName):
        if (rangeName == self.levelRange):
            return
        self.levelRange = rangeName
        self.rangeControl.choose(RANGE_NAMES.index(rangeName))
        self.nameRangeControl()
        self.levelRangeChanged.emit(rangeName)
        self.pickingRange = True
        try:
            self.refresh()
        finally:
            self.pickingRange = False

    def fetchLevels(self, key, rangeName):
        if ((key, rangeName) in self.pendingLevels or not self.isVisible()):
            return
        call = self.nextCall()
        self.pendingLevels[(key, rangeName)] = call
        self.serviceWatcher.getLevels(key, rangeName, lambda levels, error: self.onLevels(key, rangeName, levels, error, call))

    def nextCall(self):
        self.callCount += 1
        return self.callCount

    def onLevels(self, key, rangeName, levels, error, call):
        if (self.pendingLevels.get((key, rangeName)) != call):
            return
        del self.pendingLevels[(key, rangeName)]
        # close cleared the errors so the next open tries once more, and an error landing after it would undo that and start a retry for a hidden window
        if (levels is None and not self.isVisible()):
            return
        if (levels is not None):
            self.levels.setdefault(key, {})[rangeName] = levels["slots"]
            self.freshLevels.add((key, rangeName))
            self.levelErrors.pop((key, rangeName), None)
            self.levelReloadFailed.discard((key, rangeName))
        else:
            self.levelErrors[(key, rangeName)] = error
            # a refusal is asked again in seconds, so only another failure says the bars weren't refreshed
            if (rangeName in self.levels.get(key, {}) and error != serviceState.READ_ALL_LIMITED):
                self.levelReloadFailed.add((key, rangeName))
            if (error not in FINAL_ERRORS):
                self.levelRetryTimer.start(LEVEL_RETRY_MS if error == serviceState.READ_ALL_LIMITED else RETRY_MS)
        self.refresh()

    def showLoading(self):
        self.loadingShown = (self.selected, self.levelRange)
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
            self.pendingLevels.clear()
            self.levelReloadFailed.clear()
            return
        # an answer still on its way was asked before this, so it's set aside and the range asked again
        self.pendingLevels = {item: call for item, call in self.pendingLevels.items() if item[0] != key}
        # the slots stay drawn until the new answer, so a new reading never blanks the chart
        self.freshLevels = {item for item in self.freshLevels if item[0] != key}
        for item in [item for item in self.levelErrors if item[0] == key]:
            del self.levelErrors[item]

    def showChart(self, index):
        self.nameSegments()
        self.rangeControl.setVisible(index == 0)
        chart = self.charts[index]
        # a new effect on a chart replaces the old one, so a fade still running ends before that
        self.chartCrossfade.finish()
        old = next((each for each in self.charts if each.isVisible()), None)
        self.chartFade = QGraphicsOpacityEffect(chart)
        chart.setGraphicsEffect(self.chartFade)
        chart.show()
        if (old is not None and old is not chart):
            # the picture is taken while the old chart is still up
            self.chartCrossfade.start(old, int(CHART_FADE_MS * self.motionFactor), self.chartFade)
            self.rangeControl.raise_()
        else:
            self.chartFade.setEnabled(False)
        for other in self.charts:
            if (other is not chart):
                other.hide()
        mouse = self.selectedMouse()
        if (mouse is not None):
            self.updateRefreshLine(mouse.key)
            self.layoutParts()

    def updateRefreshLine(self, key):
        # the old bars stay up when a reload fails, so the line under the health box says they weren't refreshed while the level chart shows them
        rangeFailed = self.segments.index == 0 and self.levelRange != "week" and (key, self.levelRange) in self.levelReloadFailed
        self.refreshLabel.setText(NOT_REFRESHED_TEXT if key in self.notRefreshed else RANGE_NOT_REFRESHED_TEXT if rangeFailed else "")

    def closeEvent(self, event):
        # the window is kept for the next click, and so are its replies, its ranges, and any answer still on its way, so the next open shows them and isOld decides what to fetch, while an error gets one more try then
        self.errors.clear()
        self.levelErrors.clear()
        self.loadingShown = None
        self.finishAnimations()
        self.choiceMenu.hide()
        for timer in (self.fetchTimer, self.retryTimer, self.ageTimer, self.blurTimer, self.levelRetryTimer, self.loadingTimer):
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
        return mouse.historyRevision != reply.revision or upowerOld or reply.newestSlot + STALE_SLOT_SECONDS <= time.time() or reply.newestSlot > time.time() + CLOCK_BACK_SECONDS

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
        call = self.nextCall()
        self.pending[key] = call
        revision = mouse.historyRevision
        self.serviceWatcher.getHistory(key, lambda summary, error: self.onHistory(key, revision, now, summary, error, call))

    def fetchSelected(self):
        mouse = self.selectedMouse()
        if (mouse is not None and self.isVisible()):
            self.considerFetch(mouse)

    def onHistory(self, key, revision, askedAt, summary, error, call):
        if (self.pending.get(key) != call):
            return
        del self.pending[key]
        # close cleared the errors so the next open tries once more, and an error landing after it would undo that
        if (summary is None and not self.isVisible()):
            return
        if (summary is not None):
            self.replies[key] = Reply(summary, revision, askedAt)
            self.askAgainIfBehind(key, self.replies[key])
            self.forgetLevels(key)
            self.errors.pop(key, None)
            self.notRefreshed.discard(key)
        elif (key in self.replies):
            self.notRefreshed.add(key)
            self.errors[key] = error
        else:
            self.errors[key] = error
            if (error == serviceState.HISTORY_UNAVAILABLE):
                self.retryTimer.start()
        self.refresh()

    def askAgainIfBehind(self, key, reply):
        mouse = next((each for each in self.mouseList.mice if each.key == key), None)
        # the service leaves UPower's rows out of a mouse it saves over HID++, whose revision tells when its history changed
        if (mouse is None or mouse.upowerPath is None or not mouse.readFromUPower or mouse.reading is None or reply.summary.get("source") == history.SOURCE_HIDPP):
            return
        if (reply.newestLevel == (mouse.reading.percent, mouse.reading.level) or self.upowerAskedAgain.get(key) == mouse.reading):
            return
        # a UPower mouse's saves come with no revision, and a reply that ends on another level was asked for before the service saved the reading the tray holds, after a change or a service start the window may not have been there to see
        self.upowerAskedAgain[key] = mouse.reading
        self.upowerChangedAt[key] = max(self.upowerChangedAt.get(key, reply.askedAt), reply.askedAt)

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
        # the same estimate the tray icon fills to, for a rough level or a voltage
        percent = hidpp.estimatedPercent(reading)
        return None if percent is None else percent / 100

    def secondColor(self, mouse):
        reading = mouse.reading
        if (reading is None or mouse.asleep):
            return self.theme.secondary
        if (reading.charging in (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW, hidpp.CHARGING_FULL)):
            return self.theme.greenText
        percent = hidpp.estimatedPercent(reading)
        if (percent is not None and percent <= icon.RED_PERCENT):
            return self.theme.redText
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
            self.setStateIcon(None)
            return
        name = self.mouseList.displayNames()[mouse.key]
        reply = self.replies.get(mouse.key)
        self.health = health.assess(reply.summary, mouse.reading) if reply is not None else None
        state = self.iconStateFor(mouse)
        self.mouseIcon.setState(state, theme)
        self.setStateIcon(state)
        self.nameLabel.setText(name.upper())
        self.levelLabel.setText(capitalized(hidpp.levelText(mouse.reading)) if mouse.reading is not None else "")
        self.statusLabel.setText(statusWords(mouse) + (health.statusSuffix(self.health) if self.health is not None else ""))
        messageText = self.historyMessage(mouse)
        if (messageText is not None):
            self.message.setText(messageText)
            self.showParts(summary=True, history=False, paneMessage=False)
            self.fillForm(None)
        else:
            self.levelChartWasShown = self.levelChart.isVisible()
            self.showParts(summary=True, history=True, paneMessage=False)
            self.fillHealth(theme)
            self.fillCharts(reply.summary, mouse.reading, mouse.key)
            self.fillForm(self.health)
            self.updateRefreshLine(mouse.key)
        warning = self.health is not None and self.health.symbol == health.SYMBOL_WARNING
        self.setGlow(glass.stateColor(theme, mouse, warning))

    def setStateIcon(self, state):
        name = None if state is None else icon.stateIconName(state)
        if (name == self.stateIconName):
            return
        self.stateIconName = name
        # a named theme icon, since on Plasma 6.7 with Qt 6.12 a window icon sent as pixels comes out wrong wherever a pixel is partly see through, and a null icon (no mouse, or an older install without these icons) gives the window back the app's own icon
        self.setWindowIcon(QIcon() if name is None else QIcon.fromTheme(name))

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
        # bars morph only for a refresh of the bars the user was looking at, not as the window opens, under a crossfade, for another mouse, or after a message hid them
        crossfading = any(fade.animation.state() == QAbstractAnimation.State.Running for fade in (self.chartCrossfade, self.paneCrossfade))
        steady = not crossfading and not self.isQuiet() and self.levelChartKey == key and self.levelChartWasShown
        self.levelChart.morphMs = int(BAR_MORPH_MS * self.motionFactor) if steady else 0
        if (rangeName == "week"):
            # the history reply already holds the week, so the default range costs no call
            if (self.levelChartKey == key and self.levelChart.rangeName != rangeName):
                self.crossfadeLevelChart()
            self.loadingTimer.stop()
            self.levelChart.setSlots(summary.get("recent") or [], voltage, rangeName, rough=rough)
            self.levelChartKey = key
            return
        slots = self.levels.get(key, {}).get(rangeName)
        error = self.levelErrors.get((key, rangeName))
        if (slots is not None):
            if (self.loadingTimer.isActive() or (self.levelChartKey == key and self.levelChart.rangeName != rangeName)):
                self.crossfadeLevelChart()
            self.loadingTimer.stop()
            self.loadingShown = None
            self.levelChart.setSlots(slots, voltage, rangeName, rough=rough)
            self.levelChartKey = key
        elif (error in RESTART_LEVEL_ERRORS):
            self.loadingTimer.stop()
            self.levelChart.setSlots([], voltage, rangeName, message=RANGE_RESTART_TEXT)
            self.levelChartKey = key
        elif (error is not None and error != serviceState.READ_ALL_LIMITED):
            self.loadingTimer.stop()
            self.levelChart.setSlots([], voltage, rangeName, message=RANGE_ERROR_TEXT)
            self.levelChartKey = key
        elif (error is None and self.levelChartKey == key and self.levelChart.rangeName != rangeName and self.levelChart.message is None and self.loadingShown != (key, rangeName)):
            # the old bars stay up for a moment, since the reply usually comes before the word would be worth showing, while another range's message gives way at once
            if (not self.loadingTimer.isActive()):
                self.loadingTimer.start()
        else:
            # set before the call, since a reply can come back inside it and fill the chart
            self.levelChart.setSlots([], voltage, rangeName, message=LOADING_TEXT)
            self.levelChartKey = key
        if (error is None and (key, rangeName) not in self.freshLevels):
            self.fetchLevels(key, rangeName)

    def crossfadeLevelChart(self):
        # a range the user picks fades in even during the quiet after an open, while an answer that lands then swaps the bars at once
        if (not self.levelChart.isVisible() or (self.isQuiet() and not self.pickingRange)):
            return
        # a new effect on the chart replaces the old one, so a fade still running ends before that
        self.chartCrossfade.finish()
        self.levelChart.morphMs = 0
        if (self.levelChart.graphicsEffect() is not self.chartFade):
            self.chartFade = QGraphicsOpacityEffect(self.levelChart)
            self.levelChart.setGraphicsEffect(self.chartFade)
        self.chartCrossfade.start(self.levelChart, int(CHART_FADE_MS * self.motionFactor), self.chartFade)
        self.rangeControl.raise_()

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
        if (duration > 0 and self.isVisible() and self.backdrop is not None and not self.isQuiet()):
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
        leastWidth = sidebarWidth + PANE_WIDTH
        leastHeight = max(BASE_HEIGHT, min(MAX_HEIGHT, FIXED_HEIGHT + top + CHART_HEIGHT))
        room = self.screenRoom()
        if (room is not None):
            # a window never asks for more than the screen has, and what doesn't fit below the layout's own smallest size scrolls
            leastWidth = min(leastWidth, room.width())
            leastHeight = min(leastHeight, room.height())
        self.setMinimumSize(leastWidth, leastHeight)
        wantWidth, wantHeight = max(self.width(), leastWidth), max(self.height(), leastHeight)
        if (room is not None and not self.isMaximized() and not self.isFullScreen()):
            # a window sized on a bigger screen shrinks to the room here, while one that already fits keeps the size the user gave it
            wantWidth, wantHeight = max(leastWidth, min(wantWidth, room.width())), max(leastHeight, min(wantHeight, room.height()))
        if (wantWidth != self.width() or wantHeight != self.height()):
            self.resize(wantWidth, wantHeight)
        if (not self.sidebar.isVisibleTo(self) and self.width() > leastWidth and self.property("hadSidebar") and not self.isMaximized() and not self.isFullScreen()):
            # the window shrinks back by the sidebar's width once the sidebar goes
            self.resize(max(leastWidth, self.width() - SIDEBAR_WIDTH), self.height())
        self.setProperty("hadSidebar", self.sidebar.isVisibleTo(self))
        self.sidebar.setGeometry(0, 0, SIDEBAR_WIDTH, self.height())
        viewWidth = self.width() - sidebarWidth
        width = max(viewWidth, PANE_MIN_WIDTH)
        contentHeight = max(self.height(), FIXED_HEIGHT + top + CHART_MIN_HEIGHT)
        self.pane.setGeometry(sidebarWidth, 0, viewWidth, self.height())
        self.content.resize(width, contentHeight)
        self.mouseIcon.move(MARGIN - 6, MARGIN)
        textX = MARGIN + ICON_SIZE + 12
        healthX = width - MARGIN - HEALTH_WIDTH
        # a pane narrower than designed takes the room out of the text, never out of the health box
        textWidth = min(210, healthX - textX - 12)
        self.nameLabel.setGeometry(textX, MARGIN + 10, textWidth, 18)
        self.levelLabel.setGeometry(textX - 2, MARGIN + 28, textWidth + 10, 50)
        self.statusLabel.setGeometry(textX, MARGIN + 80, textWidth, 40)
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
        chartHeight = max(CHART_MIN_HEIGHT, contentHeight - chartY - 16 - FORM_HEIGHT - MARGIN)
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
        self.paneMessage.setGeometry(MARGIN, MARGIN, viewWidth - 2 * MARGIN, self.height() - 2 * MARGIN)
        self.placeScrollBars(sidebarWidth, viewWidth, width, contentHeight)

    def watchScreen(self):
        handle = self.windowHandle()
        if (handle is None or handle is self.watchedHandle):
            return
        self.watchedHandle = handle
        # a window moved to a smaller screen, or one whose scale changed, fits its new room
        handle.screenChanged.connect(self.screenChanged)
        self.watchRoom(handle.screen())

    def watchRoom(self, screen):
        # the same screen can give less room without any screenChanged, through a new resolution, a new scale, or a panel that moved
        if (screen is self.watchedScreen):
            return
        old = self.watchedScreen
        self.watchedScreen = screen
        if (old is not None):
            for signal in (old.availableGeometryChanged, old.geometryChanged, old.logicalDotsPerInchChanged):
                try:
                    signal.disconnect(self.roomChanged)
                except (TypeError, RuntimeError):
                    # an unplugged screen can be gone already
                    pass
        if (screen is not None):
            for signal in (screen.availableGeometryChanged, screen.geometryChanged, screen.logicalDotsPerInchChanged):
                signal.connect(self.roomChanged)

    def roomChanged(self, *arguments):
        self.layoutParts()

    def screenChanged(self, screen):
        self.watchRoom(screen)
        self.layoutParts()

    def placeScrollBars(self, sidebarWidth, viewWidth, contentWidth, contentHeight):
        across = contentWidth > viewWidth
        down = contentHeight > self.height()
        # a bar leaves the other axis shorter by its own width
        bar = self.vScroll.sizeHint().width()
        viewHeight = self.height() - (bar if across else 0)
        viewWidth -= bar if down else 0
        self.vScroll.setVisible(down)
        self.hScroll.setVisible(across)
        self.vScroll.setGeometry(self.width() - bar, 0, bar, viewHeight)
        self.hScroll.setGeometry(sidebarWidth, self.height() - bar, viewWidth, bar)
        self.vScroll.setRange(0, max(0, contentHeight - viewHeight))
        self.vScroll.setPageStep(max(1, viewHeight))
        self.hScroll.setRange(0, max(0, contentWidth - viewWidth))
        self.hScroll.setPageStep(max(1, viewWidth))
        self.scrollTo(self.scrollX, self.scrollY)

    def scrollTo(self, x, y):
        x = max(0, min(int(x), self.hScroll.maximum()))
        y = max(0, min(int(y), self.vScroll.maximum()))
        if (self.hScroll.value() != x):
            self.hScroll.setValue(x)
        if (self.vScroll.value() != y):
            self.vScroll.setValue(y)
        if ((x, y) == (self.scrollX, self.scrollY)):
            self.content.move(-x, -y)
            return
        self.scrollX, self.scrollY = x, y
        self.content.move(-x, -y)
        self.choiceMenu.hide()
        self.update()

    def revealFocus(self, old, new):
        if (new is None or new is self.content or not self.content.isAncestorOf(new)):
            return
        corner = new.mapTo(self.content, QPoint(0, 0))
        viewWidth = self.hScroll.pageStep()
        viewHeight = self.vScroll.pageStep()
        x, y = self.scrollX, self.scrollY
        if (corner.x() < x):
            x = corner.x() - MARGIN
        elif (corner.x() + new.width() > x + viewWidth):
            x = corner.x() + new.width() - viewWidth + MARGIN
        if (corner.y() < y):
            y = corner.y() - MARGIN
        elif (corner.y() + new.height() > y + viewHeight):
            y = corner.y() + new.height() - viewHeight + MARGIN
        self.scrollTo(x, y)

    def wheelEvent(self, event):
        if (not self.vScroll.isVisibleTo(self) and not self.hScroll.isVisibleTo(self)):
            super().wheelEvent(event)
            return
        delta = event.pixelDelta()
        if (delta.isNull()):
            # a notch is 120 units, and the bars' own step is about 3 lines
            delta = event.angleDelta() / 120 * 48
        sideways = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        dx, dy = (delta.y(), 0) if sideways else (delta.x(), delta.y())
        self.scrollTo(self.scrollX - dx, self.scrollY - dy)
        event.accept()

    def screenRoom(self):
        screen = self.screen()
        if (screen is None):
            return None
        room = screen.availableGeometry()
        frame = self.frameGeometry().height() - self.geometry().height()
        return QSize(room.width(), room.height() - (frame if frame > 0 else FRAME_ALLOWANCE))

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
        # the panels slide with the content and stop at the pane's edge
        painter.setClipRect(QRectF(offset, 0, self.width() - offset, self.height()))
        painter.translate(-self.scrollX, -self.scrollY)
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
