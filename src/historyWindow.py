import time

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, QVariantAnimation, QEasingCurve, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusMessage
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QMenu, QWidget

import charts
import dbusCalls
import glass
import health
import hidpp
import icon
import mice
import panelColor
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
STALE_SLOT_SECONDS = 2 * 60 * 60
RETRY_MS = 30 * 1000
CHART_FADE_MS = 180
PANE_FADE_MS = 180
THUMB_SLIDE_MS = 220
GLOW_FADE_MS = 400
FROST_EVERY_MS = 50
SLEEP_CHOICES = ((0, "Off"), (30, "Every 30 seconds"), (60, "Every minute"), (300, "Every 5 minutes"))
# the button shows the choice short, so the label beside it fits, while its menu names each one in full
SLEEP_SHORT = {0: "Off", 30: "30 s", 60: "1 min", 300: "5 min"}
SEGMENTS = ("Level", "Hours per Charge", "Charging")
LOADING_TEXT = "Loading"
NO_SERVICE_TEXT = "Can't reach the battery service."
NO_SERIAL_TEXT = "This mouse doesn't give a serial number, so its history can't be kept."
UNAVAILABLE_TEXT = "Its history isn't available right now."
RESTART_TEXT = "Restart Mouse Battery to see its history."
NOT_REFRESHED_TEXT = "Couldn't refresh the history."


def font(size, weight=QFont.Weight.Normal):
    chosen = QFont()
    chosen.setPixelSize(size)
    chosen.setWeight(weight)
    return chosen


def capitalized(text):
    return text[:1].upper() + text[1:]


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
    def __init__(self, summary, revision):
        self.summary = summary
        self.revision = revision
        # a UPower mouse has no revision, so a change the tray's own watcher saw marks its reply old instead
        self.upowerChanged = False
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

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.index = 0
        self.thumbX = 4.0
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self.onSlide)
        self.setFixedSize(sum(SEGMENT_WIDTHS) + 8, SEGMENT_HEIGHT)

    def segmentX(self, index):
        return 4.0 + sum(SEGMENT_WIDTHS[:index])

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
        for index in range(len(SEGMENTS)):
            if (self.segmentX(index) <= x < self.segmentX(index + 1)):
                self.choose(index)
                return

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.owner.theme
        frost = self.owner.frost()
        if (frost is not None):
            thumb = QRectF(self.thumbX, 4, SEGMENT_WIDTHS[self.index], SEGMENT_HEIGHT - 8)
            glass.paintPanel(painter, thumb, thumb.height() / 2, frost, theme, strong=True, offset=QPointF(self.mapTo(self.owner, self.rect().topLeft())))
        for index, name in enumerate(SEGMENTS):
            painter.setFont(font(12, QFont.Weight.DemiBold if index == self.index else QFont.Weight.Normal))
            painter.setPen(QColor(theme.text))
            painter.drawText(QRectF(self.segmentX(index), 0, SEGMENT_WIDTHS[index], SEGMENT_HEIGHT), int(Qt.AlignmentFlag.AlignCenter), name)


class PopupButton(QWidget):
    picked = pyqtSignal(int)

    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.seconds = 0
        self.setFixedSize(76, 24)

    def setSeconds(self, seconds):
        self.seconds = seconds
        self.update()

    def text(self):
        return SLEEP_SHORT.get(self.seconds, "Off")

    def mousePressEvent(self, event):
        menu = QMenu(self)
        for seconds, name in SLEEP_CHOICES:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(seconds == self.seconds)
            action.triggered.connect(lambda checked, value=seconds: self.pick(value))
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

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

    def __init__(self, mouseList, serviceWatcher, upowerWatcher, configHome, sessionBus, lineFor, iconStateFor, sleepCheckSeconds=0):
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
        self.selected = None
        self.wanted = None
        self.health = None
        self.panels = []
        self.makeParts(sleepCheckSeconds)
        self.fetchTimer = mice.singleShotTimer(self, 1000, self.fetchSelected)
        self.retryTimer = mice.singleShotTimer(self, RETRY_MS, self.fetchSelected)
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

    def makeParts(self, sleepCheckSeconds):
        self.sidebar = Sidebar(self)
        self.sidebar.picked.connect(self.pick)
        self.pane = QWidget(self)
        self.pane.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.paneFade = QGraphicsOpacityEffect(self.pane)
        self.paneFade.setOpacity(1.0)
        self.pane.setGraphicsEffect(self.paneFade)
        self.paneAnimation = QVariantAnimation(self)
        self.paneAnimation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.paneAnimation.valueChanged.connect(lambda value: self.paneFade.setOpacity(value))
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
        self.chartFade = QGraphicsOpacityEffect(self.levelChart)
        self.chartAnimation = QVariantAnimation(self)
        self.chartAnimation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.chartAnimation.valueChanged.connect(lambda value: self.chartFade.setOpacity(value))
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
        self.show()
        self.refresh()
        self.raise_()
        self.activateWindow()

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
        self.refresh()

    def onUPowerMouse(self, upowerMouse):
        reply = self.replies.get(upowerMouse.key)
        if (reply is not None):
            reply.upowerChanged = True
        if (upowerMouse.key == self.selected):
            self.refresh()

    def pick(self, key):
        if (key == self.selected):
            return
        self.selected = key
        duration = int(PANE_FADE_MS * self.motionFactor)
        self.paneAnimation.stop()
        if (duration > 0 and self.isVisible()):
            self.paneAnimation.setStartValue(0.0)
            self.paneAnimation.setEndValue(1.0)
            self.paneAnimation.setDuration(duration)
            self.paneAnimation.start()
        self.refresh()

    def showChart(self, index):
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
            self.chartFade.setOpacity(1.0)

    def closeEvent(self, event):
        # the window is kept for the next click, while its replies are only kept while it's open
        self.replies.clear()
        self.errors.clear()
        self.notRefreshed.clear()
        for timer in (self.fetchTimer, self.retryTimer, self.ageTimer):
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
        reply = self.replies.get(mouse.key)
        if (reply is not None and not self.isOld(mouse, reply)):
            self.scheduleAge(reply)
            return
        self.requestFetch(mouse.key)

    def isOld(self, mouse, reply):
        return mouse.historyRevision != reply.revision or reply.upowerChanged or reply.newestSlot + STALE_SLOT_SECONDS <= time.time()

    def scheduleAge(self, reply):
        # the 7 day chart and the 7 day checks keep moving, so a reply goes old two hours after its newest slot
        wait = reply.newestSlot + STALE_SLOT_SECONDS - time.time()
        # a clock that jumped would otherwise make a wait no timer can hold
        self.ageTimer.start(max(1000, int(min(wait, STALE_SLOT_SECONDS) * 1000)))

    def requestFetch(self, key):
        now = time.monotonic()
        last = self.lastFetch.get(key)
        # at most once every 30 s, so an open window stays inside the GetHistory budget
        if (last is not None and now - last < FETCH_GAP_SECONDS):
            if (not self.fetchTimer.isActive()):
                self.fetchTimer.start(max(1, int((last + FETCH_GAP_SECONDS - now) * 1000)))
            return
        mouse = next((each for each in self.mouseList.mice if each.key == key), None)
        if (mouse is None):
            return
        self.lastFetch[key] = now
        self.pending.add(key)
        revision = mouse.historyRevision
        self.serviceWatcher.getHistory(key, lambda summary, error: self.onHistory(key, revision, summary, error))

    def fetchSelected(self):
        mouse = self.selectedMouse()
        if (mouse is not None and self.isVisible()):
            self.considerFetch(mouse)

    def onHistory(self, key, revision, summary, error):
        self.pending.discard(key)
        if (summary is not None):
            self.replies[key] = Reply(summary, revision)
            self.errors.pop(key, None)
            self.notRefreshed.discard(key)
        elif (key in self.replies):
            self.notRefreshed.add(key)
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
            self.fillCharts(reply.summary, mouse.reading)
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

    def fillCharts(self, summary, reading):
        voltage = reading is not None and reading.percent is None and reading.level is None and reading.millivolts is not None
        self.levelChart.setSlots(summary.get("recent") or [], voltage)
        self.hoursChart.setSeries({"hours": self.health.hoursPerCharge})
        self.chargingChart.setSeries(self.health.chargingByPower)

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
        for child in (self.segments, self.popup, self.sidebar):
            child.update()

    def paneRect(self):
        x = SIDEBAR_WIDTH if self.sidebar.isVisible() else 0
        return QRectF(x, 0, self.width() - x, self.height())

    def ensureFrost(self):
        key = (self.width(), self.height(), self.glow.name(), self.theme.dark, self.devicePixelRatioF(), self.sidebar.isVisible())
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
        sidebarWidth = SIDEBAR_WIDTH if self.sidebar.isVisible() else 0
        healthHeight = self.healthHeight() if "health" in self.panels else HEALTH_MIN_HEIGHT
        top = max(TOP_ROW, healthHeight)
        wantedHeight = min(MAX_HEIGHT, FIXED_HEIGHT + top + CHART_HEIGHT)
        self.setMinimumSize(sidebarWidth + PANE_WIDTH, max(BASE_HEIGHT, wantedHeight))
        if (self.width() < sidebarWidth + PANE_WIDTH or self.height() < wantedHeight):
            self.resize(max(self.width(), sidebarWidth + PANE_WIDTH) if self.width() >= sidebarWidth + PANE_WIDTH else sidebarWidth + PANE_WIDTH, max(self.height(), wantedHeight))
        if (not self.sidebar.isVisible() and self.width() > PANE_WIDTH and self.minimumWidth() == PANE_WIDTH and self.property("hadSidebar")):
            # the window shrinks back by the sidebar's width once the sidebar goes
            self.resize(self.width() - SIDEBAR_WIDTH, self.height())
        self.setProperty("hadSidebar", self.sidebar.isVisible())
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
        self.layoutParts()
        super().resizeEvent(event)

    def paintEvent(self, event):
        self.ensureFrost()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawImage(QRectF(0, 0, self.width(), self.height()), self.backdrop)
        theme = self.theme
        if (self.sidebar.isVisible()):
            sidebar = QRectF(8, 8, SIDEBAR_WIDTH - 16, self.height() - 16)
            glass.paintShadow(painter, sidebar, 18, theme)
            glass.paintPanel(painter, sidebar, 18, self.frostImage, theme)
        offset = SIDEBAR_WIDTH if self.sidebar.isVisible() else 0
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
